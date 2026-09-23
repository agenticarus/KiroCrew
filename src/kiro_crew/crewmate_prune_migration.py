"""One-time startup migration: prune the crewmates an older agent sync generated.

An enrol-on-mount build of the dashboard called ``POST /api/agents/sync`` on
every chat mount, and that sync enrolled EVERY user-authored spec under
``~/.kiro/agents`` as a crewmate --
a ``config.agents`` row with no ``member_id``, on the shared ``default`` memory
store, bound to the spec by name. An existing install therefore carries one
crewmate per custom agent, most of them never opened. This module runs once at
gateway startup and:

* **removes** each such crewmate that was never chatted with as a crewmate --
  no DM thread on the Crewmates page, no session that recorded it as its
  agent, and no entry in its member activity log -- by deleting its
  ``config.agents`` row (:func:`remove_never_chatted`);
* **leaves the chatted ones exactly as they are**: on the shared ``default``
  store, with no ``member_id``. A memory binding is identity and is chosen only
  at creation; an existing member keeps its exact V1 binding (see
  ``memory-skills-hooks.md``, "Member memory experience and lifecycle"), and no
  startup pass rewrites it.

Design:

* **Precise identification.** A row is a candidate only when it is EXACTLY
  what the sync wrote: its name is its ``kiro_agent``, that spec is on disk,
  user-authored (``source == "builtin"``), not the runtime's own and not a
  crew's private copy, and every field other than ``description`` sits at its
  default -- no ``member_id``, the shared ``default`` store, no model, effort,
  triggers, colour, star, avatar or workspace, and no key the record does not
  declare (:func:`_is_fresh_sync_shape`, tested on the RAW row as
  ``config.json`` holds it, a missing key reading as its default). Both config
  layers are consulted: a name that ``config.local.json`` touches in its own
  ``agents`` section -- ``kirocrew config set --local agents.<name>.model``,
  the capability writer's overlay binding -- is the owner's and is never a
  candidate, because deleting the base row would leave the overlay leaf as a
  crewmate bound to nothing. A row the owner touched in any of those ways is
  the owner's; a hand-made crewmate has a ``member_id``; a package's spec has
  another source; a row whose spec is gone is left alone.
* **Kept on doubt, and the pass always finishes.** Removal is decided from
  three kinds of evidence, each read STRICTLY by this module -- never through
  the roster's total-by-contract readers, which answer "absent" for a damaged
  directory or file: every session file's metadata line
  (:func:`_agents_named_in_history`), the crewmate's member activity log --
  the per-session pointer ``record_activity`` appends when a chat runs as that
  member, which survives a later agent switch on the same slot
  (:func:`_activity_names_member`) -- and the crewmate's DM binding file
  (:func:`_chatted`). Any evidence the pass cannot read is treated as evidence
  FOR the crewmate: a candidate whose activity log or binding cannot be read is
  kept, and one unreadable session file keeps every candidate, because that
  file could have named any of them. The pass still completes and writes the
  marker, listing the kept-on-doubt names under ``doubted`` so the operator
  can see them; it never loops boot after boot on one bad file, and it never
  deletes on missing evidence.
* **Serialized against every session-agent writer.** The gateway clears
  ``DashboardState.crewmate_prune_settled`` BEFORE the listener binds
  (``_register_crewmate_prune_gate``) and sets it after the pass; that
  function's middleware holds every mutating request on it -- the chat send,
  slot create, slot agent switch, member thread, channel and import routes
  under ``/api/`` and the OpenAI-compatible ``POST /v1/chat/completions`` are
  all such requests -- so no session can bind an agent, and no DM binding can
  appear, between the history snapshot and a candidate's delete. Nothing else
  writes sessions while the pass runs: the cron scheduler, the Slack socket
  and the channel relaunches start after ``start_dashboard`` returns, and the
  pass completes inside it. The pass runs immediately after the bind, so the
  hold is the pass itself. Each candidate's check runs immediately before its
  own removal, never once for the whole list.
* **A refused delete is not a commit.** The delete re-tests the row inside
  the base config lock AND, nested, the overlay's own lock: the base row must
  still carry the same ``kiro_agent`` and the fresh-sync shape, and the overlay
  must still not name it. A row that changed meanwhile is refused, the pass
  writes no marker and logs which rows, and the next boot re-judges them.
* **Agent files are never touched.** Only ``config.json`` rows move. The specs
  under ``~/.kiro/agents`` and any transcript on disk stay exactly as they are.
* **Idempotent, marker-gated.** A completed pass (even a no-op) writes
  :data:`PRUNE_MARKER` under the config directory with what it did -- the same
  marker-file seam the config loader's own one-shot migrations use
  (``CONNECTIONS_UI_MIGRATION_MARKER``); the next boot finds the marker and
  returns at once. It runs from ``start_dashboard`` rather than inside the
  loader because the decision needs chat history, which only the running
  gateway has. Removed rows do not come back: nothing in the dashboard calls
  ``POST /api/agents/sync`` (``useAgents`` reads the catalog), so the rows this
  pass removes come only from installs that ran an enrol-on-mount build.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    coerce_dict_section,
    config_local_path,
    read_config_for_update,
    update_config_locked,
)
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Written under the config directory once a pass completes. Its body is the
#: record of what the pass did, so an operator can see which crewmates left
#: and which were kept because their history could not be read.
PRUNE_MARKER = "crewmate_prune_migrated.json"

#: The discovery ``source`` of a user-authored spec under ``~/.kiro/agents``
#: (``kirocrew`` = the runtime's own, ``package`` = installed by a package).
#: Tested on the SPEC the row is bound to, never on the row's own stamp.
USER_SPEC_SOURCE = "builtin"


class HistoryUnreadable(RuntimeError):
    """A piece of chat history could not be read.

    Raised by the strict readers below and caught by :func:`prune_synced_crewmates`,
    which keeps the crewmates the unreadable piece could have vouched for.
    """


@dataclasses.dataclass
class PruneReport:
    removed: list[str] = dataclasses.field(default_factory=list)
    kept: list[str] = dataclasses.field(default_factory=list)
    #: Kept because some history could not be read, with the reason per name.
    doubted: dict[str, str] = dataclasses.field(default_factory=dict)
    refused: list[str] = dataclasses.field(default_factory=list)
    skipped_marker: bool = False


def marker_path() -> Path:
    return config_dir() / PRUNE_MARKER


def _raw_agents_section(path: Path | None = None) -> dict:
    """The ``agents`` section exactly as one config file holds it.

    ``path`` defaults to ``config.json``; pass :func:`config_local_path` for the
    overlay. A file that is absent reads as an empty section; one that is
    present but unreadable raises ``ConfigReadError`` (fail closed: the pass
    cannot judge a layer it cannot see).
    """
    doc = read_config_for_update(path)
    agents = doc.get("agents") if isinstance(doc, dict) else None
    return agents if isinstance(agents, dict) else {}


def _fresh_sync_row(kiro_agent: str, description: str, source: str) -> dict:
    """What ``_do_agents_sync`` wrote for one spec: the binding, the spec's
    description and source, every other field at its default."""
    return dataclasses.asdict(
        KiroCrewAgentConfig(kiro_agent=kiro_agent, description=description, source=source)
    )


def _is_fresh_sync_shape(raw: dict, *, kiro_agent: str) -> bool:
    """Whether a RAW ``config.agents`` row is exactly a sync-written row.

    Compared field by field against :func:`_fresh_sync_row` with the row's own
    description, so a description edit alone does not disqualify (the sync
    copies it from the spec, and specs change); every other declared field must
    equal its default -- a row the owner gave a model, triggers, a colour, a
    star, an avatar or a workspace is the owner's and is never a candidate --
    and the row may carry no key the record does not declare, since an unknown
    key is something a writer other than the sync put there. A declared key the
    row lacks reads as its default: a row written by a build whose record had
    fewer fields is still the sync's row.
    """
    expected = _fresh_sync_row(kiro_agent, str(raw.get("description", "")), USER_SPEC_SOURCE)
    if set(raw) - set(expected):
        return False
    for key, default in expected.items():
        if raw.get(key, default) != default:
            return False
    return True


def _synced_candidates(cfg: KiroCrewConfig, raw_agents: dict, overlay_agents: dict) -> list[str]:
    """Names of the crewmates an older sync generated, in config order.

    ``raw_agents`` is the ``agents`` section as ``config.json`` holds it (not
    the default-filled dataclasses): the shape test must see the row the file
    holds, and the same test is re-run inside the delete's lock.
    ``overlay_agents`` is the same section from ``config.local.json``; any name
    it mentions is excluded, whatever it says about it.
    """
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_discovery import list_agents

    specs = {info.name: info for info in list_agents(agents_dir=kiro_agents_dir_path())}
    names: list[str] = []
    for name, agent in cfg.agents.items():
        if name in ("default", cfg.default_agent):
            continue
        if name in overlay_agents:
            continue
        raw = raw_agents.get(name)
        if not isinstance(raw, dict) or name != agent.kiro_agent:
            continue
        if not _is_fresh_sync_shape(raw, kiro_agent=agent.kiro_agent):
            continue
        spec = specs.get(agent.kiro_agent)
        if (
            spec is None
            or spec.source != USER_SPEC_SOURCE
            or spec.kirocrew_owned
            or spec.private_to
        ):
            continue
        if not spec.filename:
            continue
        names.append(name)
    return names


def _agents_named_in_history(conversation_log) -> set[str]:
    """Every agent any session's metadata line names -- read STRICTLY.

    ``ConversationLog.agent_usage()`` is built on ``list_sessions()``, which
    skips a file it cannot stat and swallows a first line it cannot read or
    parse; a removal must not read either as "this crewmate was never chosen".
    Here every ``*.jsonl`` in the history directory is statted, opened and its
    first line parsed, and ANY failure raises :class:`HistoryUnreadable`. A
    first line that is not a metadata record simply names no agent (that is
    the contract ``list_sessions`` applies too). Symlinks are aliases of files
    already in the walk and are skipped.

    The ``agent`` field is slot-owned and rewritten in place when the slot
    switches agents, so it names the LAST agent a session ran as; a session
    that ran as a crewmate and later switched is found through the member
    activity log instead (:func:`_activity_names_member`).
    """
    history_dir = getattr(conversation_log, "_dir", None)
    if not isinstance(history_dir, Path):
        raise HistoryUnreadable("conversation log exposes no history directory")
    named: set[str] = set()
    try:
        if not history_dir.exists():
            return named
        paths = list(history_dir.glob("*.jsonl"))
    except OSError as exc:
        raise HistoryUnreadable(f"could not list session history: {exc}") from exc
    for path in paths:
        try:
            if path.is_symlink():
                continue
            with open(path, encoding="utf-8") as fh:
                first = fh.readline().strip()
        except (OSError, UnicodeError) as exc:
            raise HistoryUnreadable(f"could not read session {path.name}: {exc}") from exc
        if not first:
            continue
        try:
            record = json.loads(first)
        except ValueError as exc:
            raise HistoryUnreadable(f"session {path.name} metadata does not parse: {exc}") from exc
        if isinstance(record, dict) and record.get("_type") == "metadata":
            agent = record.get("agent")
            if isinstance(agent, str) and agent:
                named.add(agent)
    return named


def _activity_names_member(slug: str, name: str) -> bool:
    """Whether the member activity log under ``slug`` records a session for ``name``.

    ``record_activity`` appends one ``activity/record`` event per session a chat
    ran as this member, carrying the exact member name (slugs collide) and the
    session key; it is written once per session and never rewritten, so it
    survives the slot switching to another agent afterwards. Two places hold
    it: the member event log, and -- on an install that has not yet folded it
    -- the pre-log ``activity.jsonl`` / ``activity.jsonl.1`` in the member
    directory. Both are read here, STRICTLY and read-only: nothing is created
    or folded, an event log that cannot be loaded or a legacy file that cannot
    be read or parsed raises :class:`HistoryUnreadable`. Only a log and files
    that do not exist read as "no record".
    """
    from kiro_crew import members as members_mod
    from kiro_crew.eventlog.log import MemberLog
    from kiro_crew.eventlog.types import ACTIVITY_RECORD

    try:
        log = MemberLog(slug)
        if log.exists():
            # Streamed oldest-first without retaining: the store's own read
            # path, which refuses (``LogCorrupt``) rather than guesses.
            for event in log.iter_events():
                if event.get("type") != ACTIVITY_RECORD:
                    continue
                data = event.get("data") or {}
                if data.get("member") == name and data.get("session"):
                    return True
    except Exception as exc:  # noqa: BLE001 -- a log that will not load is "unknown"
        raise HistoryUnreadable(f"could not read {name!r}'s activity log: {exc}") from exc
    try:
        base = members_mod.member_dir(slug) / members_mod.ACTIVITY_FILE_NAME
    except Exception as exc:  # noqa: BLE001
        raise HistoryUnreadable(f"could not resolve {name!r}'s activity file: {exc}") from exc
    for path in (base.with_name(base.name + ".1"), base):
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as exc:
            raise HistoryUnreadable(f"could not read {name!r}'s activity file: {exc}") from exc
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise HistoryUnreadable(
                    f"{name!r}'s activity file {path.name} does not parse: {exc}"
                ) from exc
            if isinstance(row, dict) and row.get("member") == name and row.get("session"):
                return True
    return False


def _chatted(cfg: KiroCrewConfig, name: str, named: set[str]) -> bool:
    """Whether any chat history records this crewmate.

    Three sources, any suffices: a session whose metadata names it as the agent
    (``named``, from :func:`_agents_named_in_history`); the crewmate's member
    activity log (:func:`_activity_names_member`); or the crewmate's own DM
    thread on the Crewmates page -- the thread route writes the binding file
    the first time the owner opens the thread, so a binding that names this
    crewmate IS the evidence, whether or not a message was ever sent.

    The binding is read STRICTLY here, not through ``read_dm_binding``: that
    reader is total by contract and answers "not bound" for an unreadable
    directory, an unreadable file and a malformed payload alike, which a
    removal must never mistake for "never opened". Only a binding file that
    does not exist reads as never opened. Every other failure -- the path
    cannot be resolved (a containment refusal included), the file cannot be
    statted or read, the payload does not parse -- raises
    :class:`HistoryUnreadable`, and the caller keeps the crewmate.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.atomic_write import read_bytes_with_retry

    if name in named:
        return True
    try:
        slug = members_mod.member_slug(name, cfg)
    except members_mod.MemberSlugError:
        # No slug means no DM thread and no activity log can exist; the usage
        # read above covers the rest.
        return False
    if _activity_names_member(slug, name):
        return True
    try:
        path = members_mod.dm_binding_path(slug)
    except Exception as exc:  # noqa: BLE001 -- an unresolvable path is "unknown", never "no"
        # ``MemberSlugError`` included: the slug passed ``member_slug`` above, so
        # here it means the containment check refused a path that resolves
        # outside the trust root -- a binding that may exist, not one that does not.
        raise HistoryUnreadable(f"could not resolve {name!r}'s DM binding: {exc}") from exc
    try:
        raw = read_bytes_with_retry(path)
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001 -- present but unreadable is "unknown"
        raise HistoryUnreadable(f"could not read {name!r}'s DM binding: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise HistoryUnreadable(f"{name!r}'s DM binding does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise HistoryUnreadable(f"{name!r}'s DM binding is not a record")
    # A colliding slug's binding belongs to exactly one crew name; one that
    # names another crew is legitimately not this crewmate's thread.
    return data.get("member") == name


def remove_never_chatted(cfg: KiroCrewConfig, names: list[str]) -> tuple[list[str], list[str]]:
    """Delete the ``config.agents`` rows named; returns ``(removed, refused)``.

    Each delete re-runs the candidate test on the ROWS AS THE FILES HOLD THEM,
    inside the base config lock and, nested inside it, the overlay's own lock:
    the base row must carry the same ``kiro_agent`` and still be the fresh-sync
    shape (:func:`_is_fresh_sync_shape`), and ``config.local.json`` must still
    not name it. A row that changed meanwhile -- the owner edited it, a
    member-aware write stamped it, an overlay leaf appeared -- is newer
    evidence and is refused, not deleted. The test is on identity and shape,
    never on equality with a default-filled snapshot: a row written by a build
    whose record had fewer keys must still be recognised as the sync's. Nothing
    but the base row moves: the overlay, the spec under ``~/.kiro/agents`` and
    any transcript stay.
    """
    removed: list[str] = []
    refused: list[str] = []
    for name in names:
        kiro_agent = cfg.agents[name].kiro_agent
        deleted = False

        def _mutate(doc: dict, _name: str = name, _bound: str = kiro_agent) -> dict | None:
            nonlocal deleted
            agents = coerce_dict_section(doc, "agents")
            raw = agents.get(_name)
            if not isinstance(raw, dict) or raw.get("kiro_agent") != _bound:
                return None
            if not _is_fresh_sync_shape(raw, kiro_agent=_bound):
                return None
            overlay_names_it = False

            def _peek_overlay(overlay: dict) -> None:
                nonlocal overlay_names_it
                overlay_agents = overlay.get("agents")
                overlay_names_it = isinstance(overlay_agents, dict) and _name in overlay_agents

            # Held nested so the overlay cannot gain a leaf for this name between
            # the check and the delete; ``mutate`` returning None writes nothing.
            update_config_locked(config_local_path(), mutate=_peek_overlay)
            if overlay_names_it:
                return None
            del agents[_name]
            deleted = True
            return doc

        update_config_locked(mutate=_mutate)
        if deleted:
            removed.append(name)
            del cfg.agents[name]
        else:
            refused.append(name)
    return removed, refused


def _write_marker(report: PruneReport) -> None:
    body = {
        "migrated_at": time.time(),
        "removed": report.removed,
        "kept": report.kept,
        "doubted": report.doubted,
    }
    marker = marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(marker, json.dumps(body, indent=2) + "\n")


def prune_synced_crewmates(conversation_log) -> PruneReport:
    """Run the pass once. Thread-side; safe to call on every boot.

    ``conversation_log`` is the gateway's :class:`~kiro_crew.history.ConversationLog`;
    ``None`` means history is unavailable, so every candidate is kept on doubt.
    Never raises :class:`HistoryUnreadable`: unreadable evidence keeps the
    crewmates it could have vouched for, and the pass still finishes.
    """
    report = PruneReport()
    marker = marker_path()
    if marker.exists():
        report.skipped_marker = True
        return report
    cfg = KiroCrewConfig.load()
    raw_agents = _raw_agents_section()
    overlay_agents = _raw_agents_section(config_local_path())
    candidates = _synced_candidates(cfg, raw_agents, overlay_agents)
    if candidates:
        try:
            if conversation_log is None:
                raise HistoryUnreadable("no conversation log; removal needs chat history")
            named = _agents_named_in_history(conversation_log)
        except HistoryUnreadable as exc:
            # One session file the pass cannot read could have named any
            # candidate, so every candidate is kept.
            for name in candidates:
                report.doubted[name] = str(exc)
            candidates = []
        # Check and delete ONE candidate at a time: the strict history check
        # runs immediately before its own row's removal, never once for the
        # whole list up front. The gateway holds every mutating request
        # back while the pass runs (``DashboardState.crewmate_prune_settled``,
        # armed before the listener bound), so no session can bind an agent and
        # no thread can be opened between a candidate's check and its delete.
        for name in candidates:
            try:
                chatted = _chatted(cfg, name, named)
            except HistoryUnreadable as exc:
                report.doubted[name] = str(exc)
                continue
            if chatted:
                report.kept.append(name)
            else:
                removed, refused = remove_never_chatted(cfg, [name])
                report.removed.extend(removed)
                report.refused.extend(refused)
    if report.doubted:
        logger.warning(
            "crewmate prune: %d crewmate(s) kept because their history could not be read: %s",
            len(report.doubted),
            "; ".join(f"{name}: {why}" for name, why in report.doubted.items()),
        )
    if report.refused:
        # A refused delete is not a commit: the row on disk was not the row
        # judged. Nothing is recorded as done; the next boot re-judges it.
        logger.warning(
            "crewmate prune: %d row(s) changed while being judged, pass not recorded: %s",
            len(report.refused),
            ", ".join(report.refused),
        )
        return report
    _write_marker(report)
    return report
