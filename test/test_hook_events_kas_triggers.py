"""The eleven authorable hook triggers, and the line the six new ones do not cross.

A Kiro Agent session has eleven hook triggers where the gateway has five, so the
gateway's own five were the only ones a user could author -- a hook for "the agent
just created a file" had nowhere to be written down. The six others are authorable
now, and these tests pin the three things that makes true and the two it must not
break.

What it makes true: each of the six survives create, save and a cold reload; the
form offers it; the create and update schemas accept it.

What it must not break: an event name outside the eleven is still refused at every
entry, and none of the six reaches a generated kiro-cli agent spec. kiro-cli's
``hooks`` map is a closed enum -- measured against kiro-cli 2.23.1, a spec carrying
one of these keys fails to load with "data did not match any variant of untagged
enum Repr", which would cost the user their whole default agent rather than one
hook.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    HOOK_EVENTS,
    HOOK_EVENTS_ALL,
    HOOK_EVENTS_AGENT_REQUESTED,
    HOOK_EVENTS_KAS_ONLY,
    ScriptHookStore,
    validate_hook_fields,
)
from kiro_crew.validation import (
    ALLOWED_HOOK_EVENTS,
    HOOK_CREATE_SCHEMA,
    HOOK_UPDATE_SCHEMA,
    ValidationError,
    validate_tool_args,
)

REPO = Path(__file__).resolve().parent.parent
HOOKS_PAGE = REPO / "website" / "src" / "pages" / "HooksPage.tsx"
WIRE_VALUES = REPO / "website" / "src" / "pages" / "hookEventWireValues.ts"

#: The camelCase spelling each new trigger carries on the Kiro Agent side, paired
#: with the PascalCase name the hook store persists. Written out rather than
#: derived so a rename on one side cannot quietly satisfy the other.
TWINS = {
    "preTaskExecution": "PreTaskExecution",
    "postTaskExecution": "PostTaskExecution",
    "fileCreated": "FileCreated",
    "fileEdited": "FileEdited",
    "fileDeleted": "FileDeleted",
    "userTriggered": "UserTriggered",
}


@pytest.fixture(scope="module")
def page() -> str:
    return HOOKS_PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wire_values() -> str:
    return WIRE_VALUES.read_text(encoding="utf-8")


def _skills_hook(event: str) -> None:
    """A skills-only hook definition, through the shared write-boundary validator."""
    validate_hook_fields(
        event=event,
        timeout=30,
        command="",
        skills=["deploy"],
        matcher="",
        matcher_mode="glob",
    )


def _valid(**overrides) -> dict:
    base = {"name": "h", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "command": "true"}
    base.update(overrides)
    return base


class TestTheVocabulary:
    """The three sets, and the boundary between them."""

    def test_the_fired_five_are_unchanged(self) -> None:
        """``HOOK_EVENTS`` is the promise that something calls ``fire``.

        The new triggers have no lifecycle call site, so widening this tuple
        would make the exit-code contract in ``steering-and-hooks.md`` a claim
        about events that never run.
        """
        assert HOOK_EVENTS == (
            "AgentSpawn",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "Stop",
        )

    def test_the_six_new_triggers_are_the_pascal_twins(self) -> None:
        assert set(HOOK_EVENTS_KAS_ONLY) == set(TWINS.values())
        assert len(HOOK_EVENTS_KAS_ONLY) == 6

    def test_the_two_groups_do_not_overlap(self) -> None:
        assert not set(HOOK_EVENTS) & set(HOOK_EVENTS_KAS_ONLY)
        assert len(set(HOOK_EVENTS_ALL)) == 11

    def test_the_requested_subset_is_the_two_task_triggers(self) -> None:
        """The distance to running differs inside the six.

        A Kiro Agent requests hooks by trigger name from a fixed set of seven,
        and only these two of the six are in it. The dashboard marks the two
        groups differently, so a name moving between them is a behaviour change
        and has to fail a test rather than pass quietly.
        """
        assert HOOK_EVENTS_AGENT_REQUESTED == (
            "PreTaskExecution",
            "PostTaskExecution",
        )
        assert set(HOOK_EVENTS_AGENT_REQUESTED) < set(HOOK_EVENTS_KAS_ONLY)

    def test_the_schemas_accept_exactly_the_authoring_vocabulary(self) -> None:
        """``ALLOWED_HOOK_EVENTS`` is spelled out in ``validation``, which
        ``hooks`` imports, so only a test can hold the two equal."""
        assert ALLOWED_HOOK_EVENTS == frozenset(HOOK_EVENTS_ALL)

    @pytest.mark.parametrize("camel,pascal", sorted(TWINS.items()))
    def test_each_twin_is_the_same_name_in_two_casings(self, camel: str, pascal: str) -> None:
        """The dashboard's ``normalizeEvent`` maps one to the other by
        upper-casing the first character; a pair that does not line up would
        render a Kiro-Agent-side event under an unstyled fallback badge."""
        assert camel[0].upper() + camel[1:] == pascal


class TestAuthoringTheNewTriggers:
    """Create, save, reload -- the round trip a new trigger has to survive."""

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_create_accepts_the_trigger(self, event: str, tmp_path: Path) -> None:
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid(event=event, name=f"h-{event}"))
        assert hook.event == event
        assert store.get(hook.id) is not None

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_the_trigger_reads_back_after_a_cold_reload(self, event: str, tmp_path: Path) -> None:
        """A second store instance parses the file from disk.

        The load path has its own event gate, and pointing that gate at the
        narrower fired set would quarantine the hook as unparseable -- saved,
        acknowledged, and gone on the next gateway start.
        """
        hook = ScriptHookStore(tmp_path).create(_valid(event=event, name=f"h-{event}"))
        reloaded = ScriptHookStore(tmp_path).get(hook.id)
        assert reloaded is not None, f"{event} hook did not survive the reload"
        assert reloaded.event == event
        assert reloaded.command == "true"

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_the_persisted_file_records_the_trigger_verbatim(
        self, event: str, tmp_path: Path
    ) -> None:
        ScriptHookStore(tmp_path).create(_valid(event=event, name=f"h-{event}"))
        stored = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))
        assert [h["event"] for h in stored["hooks"]] == [event]

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_update_can_move_a_hook_onto_the_trigger(self, event: str, tmp_path: Path) -> None:
        store = ScriptHookStore(tmp_path)
        hook = store.create(_valid())
        assert store.update(hook.id, {"event": event}).event == event

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_both_dashboard_schemas_accept_the_trigger(self, event: str) -> None:
        assert validate_tool_args(_valid(event=event), HOOK_CREATE_SCHEMA)["event"] == event
        assert validate_tool_args({"event": event}, HOOK_UPDATE_SCHEMA)["event"] == event


class TestTheRefusalsThatMustSurvive:
    """Widening a closed set is only safe if it is still closed."""

    @pytest.mark.parametrize(
        "event", ["bogusEvent", "fileCreated", "filecreated", "PreTaskExec", "Manual", ""]
    )
    def test_a_name_outside_the_eleven_is_still_refused(self, event: str, tmp_path: Path) -> None:
        """Including the camelCase twins: the store's own vocabulary is the
        PascalCase one, so the Kiro-Agent-side spelling is not a second accepted
        alias for the same trigger."""
        with pytest.raises(ValueError, match="invalid event"):
            ScriptHookStore(tmp_path).create(_valid(event=event))
        with pytest.raises(ValidationError):
            validate_tool_args(_valid(event=event), HOOK_CREATE_SCHEMA)

    @pytest.mark.parametrize("event", sorted(HOOK_EVENTS_KAS_ONLY))
    def test_a_skills_hook_still_refuses_the_new_triggers(self, event: str) -> None:
        """The "Load skills:" directive has a reader on two events only, and a
        new trigger does not add one."""
        with pytest.raises(ValueError, match="skills hooks cannot fire"):
            _skills_hook(event)

    @pytest.mark.parametrize("event", [HOOK_EVENT_USER_PROMPT_SUBMIT, HOOK_EVENT_AGENT_SPAWN])
    def test_a_skills_hook_still_works_on_the_two_that_read_it(self, event: str) -> None:
        _skills_hook(event)

    def test_the_five_still_behave_as_before(self, tmp_path: Path) -> None:
        store = ScriptHookStore(tmp_path)
        for i, event in enumerate(HOOK_EVENTS):
            assert store.create(_valid(event=event, name=f"h{i}")).event == event
        with pytest.raises(ValueError, match="skills hooks cannot fire"):
            _skills_hook(HOOK_EVENT_PRE_TOOL_USE)


class TestTheKiroCliSpecBoundary:
    """None of the six may reach a generated kiro-cli agent spec.

    kiro-cli 2.23.1 treats the ``hooks`` map as a closed enum: ``agent
    validate`` on a spec with a sixth key answers "data did not match any
    variant of untagged enum Repr", and ``agent list`` refuses the same file. An
    unknown TOP-LEVEL key and an unknown hook-entry field are both accepted and
    ignored, so the closed set is the event map specifically.
    """

    def test_the_new_triggers_are_not_valid_kiro_cli_events(self) -> None:
        from kiro_crew.agent import _CREW_ONLY_HOOK_EVENTS, _VALID_HOOK_EVENTS

        assert _CREW_ONLY_HOOK_EVENTS == frozenset(TWINS)
        assert not _CREW_ONLY_HOOK_EVENTS & _VALID_HOOK_EVENTS

    def test_the_generation_filter_strips_them(self) -> None:
        from kiro_crew.agent import _kiro_hooks_only

        hooks = {"agentSpawn": [{"command": "/bin/true"}]}
        hooks.update({camel: [{"command": "/bin/true"}] for camel in TWINS})
        assert set(_kiro_hooks_only(hooks)) == {"agentSpawn"}

    @pytest.mark.parametrize("camel", sorted(TWINS))
    def test_a_user_authored_trigger_is_refused_as_a_kiro_agent_trigger(
        self, camel: str, monkeypatch, caplog
    ) -> None:
        """Reported as its own refusal, not as "unknown event type".

        The name IS known -- it just cannot travel in this file -- and reporting
        it as unknown sends the reader hunting a typo that is not there. The SEL
        audit still fires, because dropping a whole bucket is a decision either
        way.
        """
        import logging

        from kiro_crew import agent as agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        sel_calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            agent_mod,
            "_sel_hook_rejected",
            lambda event, command, reason: sel_calls.append((event, command, reason)),
        )

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": {camel: [{"command": "/bin/true"}]},
                "kiro_hooks_autoimport": False,
            }
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}
        assert sel_calls, "dropping the bucket must still emit a SEL audit"
        event_tag, _command, reason = sel_calls[0]
        assert event_tag == camel
        assert reason == "Kiro Agent trigger, not emitted to kiro-cli"
        assert "unknown event type" not in caplog.text

    @pytest.mark.parametrize("camel", sorted(TWINS))
    def test_an_autoimported_script_is_refused_the_same_way(
        self, camel: str, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """A ``# event:`` header naming a Kiro Agent trigger gets that reason too.

        The autoimport scan resolves the header through the same canonical map, so
        recognising the name is what keeps it out of the "unknown event" class. The
        merge gate, not the map, is what keeps it out of the generated spec.
        """
        import logging
        import stat

        from kiro_crew import agent as agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        pascal = TWINS[camel]
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        script = hooks_dir / "formatter.sh"
        script.write_text(f"#!/bin/sh\n# event: {pascal}\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        sel_calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            agent_mod,
            "_sel_hook_rejected",
            lambda event, command, reason: sel_calls.append((event, command, reason)),
        )
        monkeypatch.setattr(agent_mod, "_DEFAULT_KIRO_HOOKS_DIR", hooks_dir)

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks": {}, "kiro_hooks_autoimport": True}}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        assert camel not in config["hooks"]
        reasons = [r for _e, _c, r in sel_calls]
        assert "unknown event header" not in reasons, f"reported as unknown: {sel_calls!r}"
        assert (
            "Kiro Agent trigger, not emitted to kiro-cli" in reasons
        ), f"expected the Kiro Agent reason; got {sel_calls!r}"


class TestTheFormOffersThem:
    """The authoring surface, pinned to the code the way ``MATCHER_MODES`` is."""

    def test_the_picker_offers_every_authorable_event_in_order(self, wire_values: str) -> None:
        declared = re.search(r"export const EVENTS = \[(.*?)\]", wire_values, re.S)
        assert declared, "EVENTS no longer declared in hookEventWireValues.ts"
        offered = re.findall(r"'([^']+)'", declared.group(1))
        assert offered == list(HOOK_EVENTS_ALL), (
            f"hookEventWireValues offers {offered}; hooks.HOOK_EVENTS_ALL "
            f"ships {list(HOOK_EVENTS_ALL)}"
        )

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("KAS_ONLY_EVENTS", HOOK_EVENTS_KAS_ONLY),
            ("AGENT_REQUESTED_EVENTS", HOOK_EVENTS_AGENT_REQUESTED),
        ],
    )
    def test_the_marker_reads_the_split_from_the_same_tuples(
        self, wire_values: str, name: str, expected: tuple[str, ...]
    ) -> None:
        """The dormant mark on the picker option and the row is derived, not typed.

        Two marks distinguish the six, and a hand-maintained copy of either set
        would let the dashboard claim a trigger is awaiting an agent that never
        asks for it. The round that ships delivery moves a name out of a tuple
        here and both surfaces follow.
        """
        declared = re.search(rf"export const {name} = \[(.*?)\]", wire_values, re.S)
        assert declared, f"{name} no longer declared in hookEventWireValues.ts"
        listed = re.findall(r"'([^']+)'", declared.group(1))
        assert listed == list(
            expected
        ), f"{name} lists {listed}; the backend tuple ships {list(expected)}"

    @pytest.mark.parametrize("const", ["EVENT_STYLE", "EVENT_BADGE"])
    def test_every_event_has_its_own_badge(self, page: str, const: str) -> None:
        """A missing key falls through to an unstyled grey pill, which reads as
        "this event is not recognised" on a row the form itself created."""
        block = re.search(rf"const {const}: Record<[^>]+> = \{{(.*?)\n\}}", page, re.S)
        assert block, f"{const} no longer declared in HooksPage.tsx"
        keyed = set(re.findall(r"(\w+):", block.group(1)))
        assert keyed == set(HOOK_EVENTS_ALL), f"{const} is missing {set(HOOK_EVENTS_ALL) - keyed}"
