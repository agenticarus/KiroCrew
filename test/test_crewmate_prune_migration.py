"""The one-time startup prune of sync-generated crewmates (``crewmate_prune_migration``).

Seeds an old-style ``config.json`` -- the rows an older ``POST /api/agents/sync``
left behind (no ``member_id``, shared ``default`` store, bound to the user's own
spec by name) beside a hand-created member and a sync-shaped row that owns a
non-empty memory store -- and asserts the pass does exactly what its docstring
promises: the never-chatted synced row on the shared store is removed, the
chatted one keeps its exact binding (shared store, no ``member_id``), the
hand-created one and the one with memory are untouched, the marker is written,
and a second boot is a no-op. Then the kept-on-doubt edges: history the pass
cannot read keeps the crewmates it could have vouched for, names them in the
marker, and the pass still finishes; a spec that is gone, a package spec, the
runtime's own, a row with an extra key or one the overlay touches is never a
candidate. Last, the request gate the pass relies on: registering it arms the
barrier, every mutating request -- under ``/api/`` or not -- waits until the
pass settles (or gets 503 when it never does), reads are never held.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import crewmate_prune_migration as mig
from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.memory_stores import provision_member_memory


def _spec(name: str, **kw) -> AgentInfo:
    base = dict(name=name, filename=f"{name}.json", description="", model="auto", source="builtin")
    base.update(kw)
    return AgentInfo(**base)


def _synced(name: str) -> KiroCrewAgentConfig:
    # Exactly what an older sync wrote.
    return KiroCrewAgentConfig(kiro_agent=name, description=f"{name} agent", source="builtin")


class _Log:
    """A conversation log: ``_dir`` holds one ``<key>.jsonl`` per session, the
    first line the metadata record the real log writes."""

    def __init__(self, root: Path):
        self._dir = root
        root.mkdir(parents=True, exist_ok=True)

    def session(self, key: str, agent: str | None = None, *, raw: bytes | None = None):
        path = self._dir / f"{key}.jsonl"
        if raw is not None:
            path.write_bytes(raw)
            return path
        meta = {"_type": "metadata", "title": key}
        if agent:
            meta["agent"] = agent
        path.write_text(
            json.dumps(meta) + "\n" + json.dumps({"role": "user", "content": "hi"}) + "\n"
        )
        return path


@pytest.fixture
def log(tmp_path):
    return _Log(tmp_path / "sessions")


@pytest.fixture
def bindings_dir(tmp_path, monkeypatch):
    """Point the DM-binding path at a scratch dir; ``write(name)`` opens a thread."""
    root = tmp_path / "dm"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.members.dm_binding_path", lambda slug: root / f"{slug}.json")
    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, cfg=None: name)

    def write(name: str, *, member: str | None = None, raw: bytes | None = None):
        path = root / f"{name}.json"
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(json.dumps({"slot_key": f"member-{name}", "member": member or name}))
        return path

    return write


@pytest.fixture
def activity_dir(tmp_path, monkeypatch):
    """Point the member directory at a scratch dir; ``write(name)`` records a session."""
    root = tmp_path / "members"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.members.member_dir", lambda slug: root / slug)
    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, cfg=None: name)

    def write(name: str, *, member: str | None = None, raw: bytes | None = None, rotated=False):
        d = root / name
        d.mkdir(exist_ok=True)
        path = d / ("activity.jsonl.1" if rotated else "activity.jsonl")
        if raw is not None:
            path.write_bytes(raw)
        else:
            row = {"ts": "2026-01-01T00:00:00Z", "member": member or name, "session": f"s-{name}"}
            path.write_text(json.dumps(row) + "\n")
        return path

    return write


@pytest.fixture
def old_style_config():
    """Three rows: two the sync left (one chatted, one never), one made by hand."""
    cfg = KiroCrewConfig.load()
    cfg.agents["radar"] = _synced("radar")
    cfg.agents["scout"] = _synced("scout")
    cfg.agents["by-hand"] = KiroCrewAgentConfig(kiro_agent="radar", description="mine")
    provision_member_memory(cfg, "by-hand")
    # A row that was provisioned (own store, so `memory_store != "default"`)
    # but whose member_id is blank: not the never-provisioned signature, so not
    # a candidate whatever its history. No directory is inspected to decide that.
    cfg.agents["kept-memory"] = _synced("kept-memory")
    store = provision_member_memory(cfg, "kept-memory")
    cfg.agents["kept-memory"].member_id = ""  # back to the sync's shape, store kept
    cfg.save()
    from kiro_crew.memory_stores import _named_store_dir

    (_named_store_dir(store) / "memory" / "note.md").write_text("kept\n")
    hand = KiroCrewConfig.load().agents["by-hand"]
    assert hand.member_id and hand.memory_store != "default"
    return {"radar": _spec("radar"), "scout": _spec("scout"), "kept-memory": _spec("kept-memory")}


def _run(specs: dict, log):
    with (
        patch("kiro_crew.agent_discovery.list_agents", return_value=list(specs.values())),
        patch("kiro_crew.agent.kiro_agents_dir_path", return_value="/nowhere"),
    ):
        return mig.prune_synced_crewmates(log)


class TestThePass:
    def test_removes_never_chatted_keeps_chatted_leaves_hand_made(
        self, old_style_config, bindings_dir, log
    ):
        before = KiroCrewConfig.load()
        hand_before = before.agents["by-hand"]
        assert not mig.marker_path().exists()
        bindings_dir("radar")  # the owner opened radar's thread once
        log.session("chat-1", "kirocrew")

        report = _run(old_style_config, log)

        assert report.removed == ["scout"]
        assert report.kept == ["radar"]
        after = KiroCrewConfig.load()
        assert "scout" not in after.agents
        # The chatted row keeps its exact binding: a memory binding is identity,
        # chosen at creation, and no startup pass rewrites it.
        assert after.agents["radar"] == before.agents["radar"]
        assert after.agents["radar"].member_id == ""
        assert after.agents["radar"].memory_store == "default"
        assert after.agents["by-hand"] == hand_before
        assert after.agents["kept-memory"] == before.agents["kept-memory"]
        assert after.agents["kept-memory"].memory_store != "default"
        marker = json.loads(mig.marker_path().read_text())
        assert marker["removed"] == ["scout"]
        assert marker["kept"] == ["radar"]
        assert marker["doubted"] == {}

    def test_an_activity_record_counts_as_chatted(
        self, old_style_config, bindings_dir, activity_dir, log
    ):
        # The slot later switched agents, so no session metadata names scout
        # and no DM thread exists; the member activity log still holds the
        # session pointer record_activity wrote when the chat ran as scout.
        log.session("chat-3", "someone-else")
        activity_dir("scout")
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert report.kept == ["scout"]
        assert KiroCrewConfig.load().agents["scout"].member_id == ""

    def test_a_rotated_activity_file_counts_too(
        self, old_style_config, bindings_dir, activity_dir, log
    ):
        activity_dir("scout", rotated=True)
        report = _run(old_style_config, log)
        assert report.kept == ["scout"]

    def test_an_event_log_record_counts_as_chatted(
        self, old_style_config, bindings_dir, activity_dir, log, monkeypatch
    ):
        class _FakeLog:
            def __init__(self, slug):
                self.slug = slug

            def exists(self):
                return self.slug == "scout"

            def iter_events(self):
                yield {"type": "activity/record", "data": {"member": "scout", "session": "s"}}

        monkeypatch.setattr("kiro_crew.eventlog.log.MemberLog", _FakeLog)
        report = _run(old_style_config, log)
        assert report.kept == ["scout"]
        assert report.removed == ["radar"]

    def test_an_activity_record_for_another_name_is_not_this_crewmates(
        self, old_style_config, bindings_dir, activity_dir, log
    ):
        # Slugs collide: one log can hold two members. The name decides.
        activity_dir("scout", member="Scout!")
        report = _run(old_style_config, log)
        assert "scout" in report.removed

    def test_a_session_that_named_the_crewmate_counts_as_chatted(
        self, old_style_config, bindings_dir, log
    ):
        # No DM thread, but a plain session selected it: kept, untouched.
        log.session("chat-2", "scout")
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert report.kept == ["scout"]
        assert KiroCrewConfig.load().agents["scout"].member_id == ""

    def test_second_boot_is_a_no_op(self, old_style_config, bindings_dir, log):
        bindings_dir("radar")
        _run(old_style_config, log)
        cfg = KiroCrewConfig.load()
        cfg.agents["late"] = _synced("late")
        cfg.save()
        specs = dict(old_style_config, late=_spec("late"))
        report = _run(specs, log)
        assert report.skipped_marker is True
        assert "late" in KiroCrewConfig.load().agents

    def test_a_no_op_pass_still_writes_the_marker(self, bindings_dir, log):
        report = _run({}, log)
        assert report.removed == [] and report.kept == []
        assert mig.marker_path().exists()


class TestKeptOnDoubt:
    """Unreadable evidence keeps the crewmates it could have vouched for; the
    pass still finishes and the marker names them, so no boot re-runs it."""

    def test_a_session_file_that_does_not_parse_keeps_every_candidate(
        self, old_style_config, bindings_dir, log
    ):
        # list_sessions() would skip this file; the prune must not. It could
        # have named any candidate, so all of them are kept.
        log.session("broken", raw=b"{not json\n")
        report = _run(old_style_config, log)
        assert report.removed == []
        assert set(report.doubted) == {"radar", "scout"}
        assert "broken" in report.doubted["scout"]
        after = KiroCrewConfig.load()
        assert {"radar", "scout", "by-hand"} <= set(after.agents)
        assert after.agents["radar"].member_id == ""
        marker = json.loads(mig.marker_path().read_text())
        assert set(marker["doubted"]) == {"radar", "scout"}

    def test_a_doubted_pass_is_recorded_and_not_re_run(self, old_style_config, bindings_dir, log):
        log.session("binary", raw=b"\xff\xfe\n")
        _run(old_style_config, log)
        assert "scout" in KiroCrewConfig.load().agents
        report = _run(old_style_config, log)
        assert report.skipped_marker is True

    def test_a_session_file_without_metadata_names_nobody(
        self, old_style_config, bindings_dir, log
    ):
        # A first line that is not a metadata record names no agent; that is
        # the same contract list_sessions applies, and it is not an error.
        log.session("plain", raw=b'{"role": "user", "content": "x"}\n')
        report = _run(old_style_config, log)
        assert set(report.removed) == {"radar", "scout"}

    def test_no_conversation_log_keeps_every_candidate(self, old_style_config, bindings_dir):
        report = _run(old_style_config, None)
        assert report.removed == []
        assert set(report.doubted) == {"radar", "scout"}
        assert "scout" in KiroCrewConfig.load().agents
        assert mig.marker_path().exists()

    def test_a_malformed_binding_file_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log
    ):
        # The roster's own reader answers "not bound" for this file; the prune
        # must not: a damaged member directory is unknown history, not none.
        # Only scout's evidence is in doubt, so only scout is kept on it.
        bindings_dir("scout", raw=b"{not json")
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert list(report.doubted) == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert json.loads(mig.marker_path().read_text())["doubted"] == report.doubted

    def test_an_unreadable_binding_file_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log
    ):
        bindings_dir("scout", raw=b"\xff\xfe")  # not UTF-8
        report = _run(old_style_config, log)
        assert list(report.doubted) == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents

    def test_an_unresolvable_binding_path_keeps_the_crewmate(
        self, old_style_config, activity_dir, monkeypatch, log
    ):
        def _boom(slug):
            raise OSError("members root unreadable")

        monkeypatch.setattr("kiro_crew.members.dm_binding_path", _boom)
        report = _run(old_style_config, log)
        assert set(report.doubted) == {"radar", "scout"}
        assert "scout" in KiroCrewConfig.load().agents

    def test_a_binding_path_refused_by_containment_keeps_the_crewmate(
        self, old_style_config, activity_dir, monkeypatch, log
    ):
        # A slug that passed ``member_slug`` but whose binding path resolves
        # outside the trust root (a symlinked component) is a binding that MAY
        # exist, not one that does not.
        from kiro_crew.members import MemberSlugError

        def _escapes(slug):
            raise MemberSlugError(f"member slug {slug!r} escapes root")

        monkeypatch.setattr("kiro_crew.members.dm_binding_path", _escapes)
        report = _run(old_style_config, log)
        assert set(report.doubted) == {"radar", "scout"}
        assert "scout" in KiroCrewConfig.load().agents

    def test_an_unparseable_activity_file_keeps_that_crewmate(
        self, old_style_config, bindings_dir, activity_dir, log
    ):
        activity_dir("scout", raw=b"{torn")
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert list(report.doubted) == ["scout"]

    def test_a_corrupt_event_log_keeps_that_crewmate(
        self, old_style_config, bindings_dir, activity_dir, log, monkeypatch
    ):
        class _Corrupt:
            def __init__(self, slug):
                self.slug = slug

            def exists(self):
                return self.slug == "scout"

            def iter_events(self):
                raise RuntimeError("committed region unreadable")

        monkeypatch.setattr("kiro_crew.eventlog.log.MemberLog", _Corrupt)
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert list(report.doubted) == ["scout"]

    def test_a_binding_for_another_name_is_not_this_crewmates(
        self, old_style_config, bindings_dir, log
    ):
        # A colliding slug's file names the other crew: scout was never opened.
        bindings_dir("scout", member="someone-else")
        bindings_dir("radar")
        report = _run(old_style_config, log)
        assert report.removed == ["scout"]

    def test_a_doubt_on_one_candidate_does_not_spare_the_next(
        self, old_style_config, bindings_dir, log
    ):
        # Check-then-delete is per candidate: radar's binding is in doubt and
        # radar stays; scout's evidence is clean and scout is judged on it.
        bindings_dir("radar", raw=b"{not json")
        report = _run(old_style_config, log)
        assert list(report.doubted) == ["radar"]
        assert report.removed == ["scout"]
        after = KiroCrewConfig.load()
        assert "radar" in after.agents and "scout" not in after.agents

    def test_a_row_that_changed_under_the_lock_is_refused_and_no_marker(
        self, old_style_config, bindings_dir, log
    ):
        bindings_dir("radar")
        # The on-disk row gained a model between the judgement and the lock:
        # newer evidence wins, the delete is refused, and a refusal is not a
        # commit -- no marker, so the next boot re-judges it.
        original = mig.remove_never_chatted

        def _edit_then_remove(cfg_, names):
            live = KiroCrewConfig.load()
            live.agents["scout"].model = "some-model"
            live.save()
            return original(cfg_, names)

        with patch.object(mig, "remove_never_chatted", _edit_then_remove):
            report = _run(old_style_config, log)
        assert report.removed == []
        assert report.refused == ["scout"]
        assert KiroCrewConfig.load().agents["scout"].model == "some-model"
        assert not mig.marker_path().exists()

    def test_a_legacy_row_with_fewer_keys_is_still_removed(
        self, old_style_config, bindings_dir, log
    ):
        # A build whose record had no member_id wrote rows without that key;
        # the delete's fence is identity and shape, not key-for-key equality.
        from kiro_crew.config.loader import read_config_for_update, update_config_locked

        def _strip(doc):
            row = doc["agents"]["scout"]
            for key in ("member_id", "starred", "session_color", "avatar", "reasoning_effort"):
                row.pop(key, None)
            return doc

        update_config_locked(mutate=_strip)
        assert "member_id" not in read_config_for_update()["agents"]["scout"]
        bindings_dir("radar")
        report = _run(old_style_config, log)
        assert report.removed == ["scout"]
        assert mig.marker_path().exists()


class TestCandidates:
    def test_only_untouched_user_spec_rows(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["radar"] = _synced("radar")  # candidate
        cfg.agents["gone"] = _synced("gone")  # spec absent from disk
        cfg.agents["omni"] = KiroCrewAgentConfig(kiro_agent="omni", source="package")
        cfg.agents["own"] = KiroCrewAgentConfig(
            kiro_agent="own", source="builtin"
        )  # kirocrew-owned spec
        cfg.agents["copy"] = KiroCrewAgentConfig(
            kiro_agent="copy", source="builtin"
        )  # private copy
        specs = {
            "radar": _spec("radar"),
            "omni": _spec("omni", filename="Pkg-omni.json", source="package", package="Pkg"),
            "own": _spec("own", kirocrew_owned=True),
            "copy": _spec("copy", private_to="someone"),
        }
        cfg.agents["tuned"] = KiroCrewAgentConfig(kiro_agent="tuned", source="builtin", model="m")
        cfg.agents["routed"] = KiroCrewAgentConfig(
            kiro_agent="routed", source="builtin", triggers="x"
        )
        cfg.agents["renamed"] = _synced("radar")  # name != kiro_agent: not the sync's row
        specs["tuned"] = _spec("tuned")
        specs["routed"] = _spec("routed")
        cfg.save()
        raw = mig._raw_agents_section()
        with (
            patch("kiro_crew.agent_discovery.list_agents", return_value=list(specs.values())),
            patch("kiro_crew.agent.kiro_agents_dir_path", return_value="/nowhere"),
        ):
            assert mig._synced_candidates(cfg, raw, {}) == ["radar"]

    def test_a_row_the_overlay_touches_is_never_a_candidate(self):
        # ``kirocrew config set --local agents.radar.model m`` leaves the base
        # row pristine and puts one leaf in config.local.json; deleting the
        # base row would leave that leaf as a crewmate bound to nothing.
        from kiro_crew.config.loader import config_local_path

        cfg = KiroCrewConfig.load()
        cfg.agents["radar"] = _synced("radar")
        cfg.agents["scout"] = _synced("scout")
        cfg.save()
        config_local_path().write_text(json.dumps({"agents": {"radar": {"model": "m"}}}))
        cfg = KiroCrewConfig.load()
        raw = mig._raw_agents_section()
        overlay = mig._raw_agents_section(config_local_path())
        assert overlay == {"radar": {"model": "m"}}
        specs = {"radar": _spec("radar"), "scout": _spec("scout")}
        with (
            patch("kiro_crew.agent_discovery.list_agents", return_value=list(specs.values())),
            patch("kiro_crew.agent.kiro_agents_dir_path", return_value="/nowhere"),
        ):
            assert mig._synced_candidates(cfg, raw, overlay) == ["scout"]

    def test_an_overlay_leaf_appearing_under_the_lock_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        from kiro_crew.config.loader import config_local_path

        original = mig.remove_never_chatted

        def _overlay_then_remove(cfg_, names):
            config_local_path().write_text(json.dumps({"agents": {"scout": {"model": "m"}}}))
            return original(cfg_, names)

        with patch.object(mig, "remove_never_chatted", _overlay_then_remove):
            report = _run(old_style_config, log)
        assert report.refused == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert not mig.marker_path().exists()

    def test_a_key_the_record_does_not_declare_disqualifies(self):
        raw = {"kiro_agent": "radar", "description": "", "source": "builtin", "extra": 1}
        assert not mig._is_fresh_sync_shape(raw, kiro_agent="radar")

    def test_a_description_edit_alone_keeps_a_row_a_candidate(self):
        # The sync copies the spec's description and specs change; the other
        # fields are the owner's signal.
        raw = {"kiro_agent": "radar", "description": "rewritten", "source": "builtin"}
        assert mig._is_fresh_sync_shape(raw, kiro_agent="radar")
        assert not mig._is_fresh_sync_shape({**raw, "starred": True}, kiro_agent="radar")
        assert not mig._is_fresh_sync_shape(
            {**raw, "avatar": {"kind": "image"}}, kiro_agent="radar"
        )
        assert not mig._is_fresh_sync_shape({**raw, "workspace": "other"}, kiro_agent="radar")
        assert not mig._is_fresh_sync_shape({**raw, "member_id": "m1"}, kiro_agent="radar")
        assert not mig._is_fresh_sync_shape({**raw, "memory_store": "member-x"}, kiro_agent="radar")


class TestTheGate:
    """``_register_crewmate_prune_gate``: armed before bind, holds writers only.

    A stub ``state`` carrying just the event stands in for ``DashboardState``;
    the gate reads nothing else. The timeout is patched down so the 503 path
    runs in milliseconds.
    """

    @staticmethod
    def _app():
        import asyncio
        from types import SimpleNamespace

        from aiohttp import web

        from kiro_crew.dashboard import server as server_mod

        event = asyncio.Event()
        event.set()
        state = SimpleNamespace(crewmate_prune_settled=event)
        app = web.Application()
        hits: list[str] = []

        async def _write(request):
            hits.append(request.method)
            return web.json_response({"ok": True})

        app.router.add_post("/api/chat/slots/s1/agent", _write)
        app.router.add_get("/api/chat/slots", _write)
        app.router.add_post("/v1/chat/completions", _write)
        server_mod._register_crewmate_prune_gate(app, state)
        return app, state, hits

    def test_registering_arms_the_barrier(self):
        _app, state, _hits = self._app()
        assert not state.crewmate_prune_settled.is_set()

    @pytest.mark.asyncio
    async def test_a_mutating_api_request_waits_until_the_pass_settles(self):
        import asyncio

        from aiohttp.test_utils import TestClient, TestServer

        app, state, hits = self._app()
        async with TestClient(TestServer(app)) as client:
            pending = asyncio.ensure_future(client.post("/api/chat/slots/s1/agent"))
            await asyncio.sleep(0.05)
            assert hits == []  # held, not refused
            state.crewmate_prune_settled.set()
            resp = await pending
            assert resp.status == 200
            assert hits == ["POST"]

    @pytest.mark.asyncio
    async def test_reads_are_never_held(self):
        from aiohttp.test_utils import TestClient, TestServer

        app, _state, hits = self._app()
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/chat/slots")).status == 200
        assert hits == ["GET"]

    @pytest.mark.asyncio
    async def test_a_write_outside_api_is_held_too(self, monkeypatch):
        # ``POST /v1/chat/completions`` binds a session's agent like any
        # dashboard route; the gate keys on the method, never on the path.
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard import server as server_mod

        monkeypatch.setattr(server_mod, "_CREWMATE_PRUNE_GATE_TIMEOUT_S", 0.05)
        app, _state, hits = self._app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/v1/chat/completions")
            assert resp.status == 503
        assert hits == []

    @pytest.mark.asyncio
    async def test_a_pass_that_never_settles_answers_503(self, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard import server as server_mod

        monkeypatch.setattr(server_mod, "_CREWMATE_PRUNE_GATE_TIMEOUT_S", 0.05)
        app, _state, hits = self._app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/agent")
            assert resp.status == 503
            assert (await resp.json())["code"] == "prune_in_progress"
        assert hits == []

    @pytest.mark.asyncio
    async def test_once_settled_every_request_passes(self):
        from aiohttp.test_utils import TestClient, TestServer

        app, state, hits = self._app()
        state.crewmate_prune_settled.set()
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/chat/slots/s1/agent")).status == 200
        assert hits == ["POST"]
