"""A spec's ``hooks`` field accepts Crew's object form and KAS's array form.

Four properties are pinned here:

* an array-form ``hooks`` value round-trips through the shared normalizer, with
  the fields kiro-cli cannot express still present in the stored spec;
* an object-form value serializes for kiro-cli byte for byte as it does without
  the array form existing, and does not go through the new normalizer at all;
* the same defect expressed in either shape is rejected by the same error path,
  with the same SEL reason;
* every guard the array form adds has a test that fails once that guard is gone.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.agent import (
    _MAX_HOOK_DESCRIPTION_LEN,
    _MAX_HOOK_NAME_LEN,
    _MAX_HOOK_PAYLOAD_LEN,
    _MAX_SPEC_HOOK_DOCUMENTS,
    _apply_user_kiro_hooks,
    hook_documents_to_object_form,
    normalize_spec_hooks,
)


def _hook_script(tmp_path: Path, name: str = "hook.sh") -> str:
    """Create an executable script and return its absolute path."""
    script = tmp_path / "hooks" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return str(script)


def _mc_cfg(kiro_hooks: object) -> dict:
    return {"agent": {"kiro_hooks": kiro_hooks, "kiro_hooks_autoimport": False}}


def _apply(kiro_hooks: object, *, bundled: dict | None = None) -> dict:
    """Run the merge over *kiro_hooks* and return the resulting hooks object."""
    config: dict = {"hooks": dict(bundled or {})}
    _apply_user_kiro_hooks(config, _mc_cfg(kiro_hooks))
    return config["hooks"]


@pytest.fixture()
def sel_calls(monkeypatch) -> list[tuple[str, str, str]]:
    """Capture every ``_sel_hook_rejected`` call as ``(event, command, reason)``."""
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        agent_mod,
        "_sel_hook_rejected",
        lambda event, command, reason: calls.append((event, command, reason)),
    )
    return calls


class TestArrayFormRoundTrip:
    """An array-form value survives load -> store -> load unchanged."""

    def _document(self, command: str) -> dict:
        return {
            "name": "guard",
            "description": "guards the shell",
            "trigger": "PreToolUse",
            "matcher": "execute_bash",
            "action": {"type": "command", "command": command},
            "timeout": 30,
            "enabled": True,
            "confirm": False,
        }

    def test_document_survives_normalize_store_normalize(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        first = normalize_spec_hooks([self._document(command)])
        # Store is the spec on disk: serialize the normalized form and read it
        # back through the same helper.
        stored = json.loads(json.dumps(first))
        second = normalize_spec_hooks(stored)
        assert first == second
        assert first == [self._document(command)]

    def test_normalizer_keeps_the_fields_kiro_cli_cannot_express(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        (doc,) = normalize_spec_hooks([self._document(command)])
        for field in ("name", "description", "timeout", "enabled", "confirm"):
            assert field in doc, f"{field} must stay in the stored spec"

    def test_agent_action_survives_normalization(self) -> None:
        (doc,) = normalize_spec_hooks(
            [
                {
                    "name": "ask",
                    "trigger": "Manual",
                    "action": {"type": "agent", "prompt": "summarize the diff"},
                }
            ]
        )
        assert doc["action"] == {"type": "agent", "prompt": "summarize the diff"}
        assert doc["trigger"] == "Manual"

    def test_array_form_reaches_the_generated_spec(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        hooks = _apply([self._document(command)])
        assert hooks == {"preToolUse": [{"command": command, "matcher": "execute_bash"}]}

    @pytest.mark.parametrize(
        ("trigger", "event"),
        [
            ("PreToolUse", "preToolUse"),
            ("preToolUse", "preToolUse"),
            ("PostToolUse", "postToolUse"),
            ("UserPromptSubmit", "userPromptSubmit"),
            ("SessionStart", "agentSpawn"),
            ("agentSpawn", "agentSpawn"),
            ("Stop", "stop"),
        ],
    )
    def test_expressible_triggers_map_to_kiro_cli_events(
        self, tmp_path: Path, trigger: str, event: str
    ) -> None:
        command = _hook_script(tmp_path)
        hooks = _apply(
            [{"name": "h", "trigger": trigger, "action": {"type": "command", "command": command}}]
        )
        assert hooks == {event: [{"command": command}]}

    @pytest.mark.parametrize(
        "trigger",
        [
            "PreTaskExec",
            "preTaskExecution",
            "PostTaskExec",
            "PostFileCreate",
            "fileCreated",
            "PostFileSave",
            "fileEdited",
            "PostFileDelete",
            "fileDeleted",
            "Manual",
            "userTriggered",
        ],
    )
    def test_triggers_kiro_cli_cannot_name_are_dropped_on_emission_only(
        self, tmp_path: Path, trigger: str
    ) -> None:
        command = _hook_script(tmp_path)
        document = {
            "name": "h",
            "trigger": trigger,
            "action": {"type": "command", "command": command},
        }
        assert normalize_spec_hooks([document]), "the document must stay in the stored spec"
        assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert _apply([document]) == {}

    def test_agent_action_is_dropped_on_emission_only(self) -> None:
        document = {
            "name": "ask",
            "trigger": "PreToolUse",
            "action": {"type": "agent", "prompt": "check the plan"},
        }
        assert normalize_spec_hooks([document])
        assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}

    def test_standalone_file_wrapper_is_not_a_spec_shape(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        wrapper = {
            "version": "v1",
            "hooks": [
                {
                    "name": "h",
                    "trigger": "PreToolUse",
                    "action": {"type": "command", "command": command},
                }
            ],
        }
        assert normalize_spec_hooks(wrapper) == []
        assert _apply(wrapper) == {}
        assert normalize_spec_hooks(wrapper.get("hooks")), "the inner array is a spec value"

    def test_a_value_that_is_not_an_array_is_ignored_with_an_audit(self, sel_calls) -> None:
        assert normalize_spec_hooks("preToolUse") == []
        assert [c for c in sel_calls if "not an array of hook documents" in c[2]]

    @pytest.mark.parametrize("value", ["preToolUse", 7, True])
    def test_the_merge_path_audits_a_value_of_neither_shape(self, sel_calls, value: object) -> None:
        """A string or number `kiro_hooks` is audited, not dropped in silence."""
        assert _apply(value) == {}
        assert [c for c in sel_calls if "not an array of hook documents" in c[2]]


class TestObjectFormUnchanged:
    """The object form serializes for kiro-cli exactly as it always has."""

    def test_serialized_bytes_are_pinned(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        hooks = _apply(
            {"preToolUse": [{"matcher": "*", "command": command}]},
            bundled={"postToolUse": [{"matcher": "execute_bash", "command": "audit.sh"}]},
        )
        expected = (
            '{"postToolUse": [{"matcher": "execute_bash", "command": "audit.sh"}], '
            '"preToolUse": [{"command": ' + json.dumps(command) + ', "matcher": "*"}]}'
        )
        assert json.dumps(hooks) == expected

    def test_extra_entry_keys_are_still_stripped(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        hooks = _apply({"preToolUse": [{"command": command, "timeout_ms": 5, "cache_ttl": 9}]})
        assert hooks == {"preToolUse": [{"command": command}]}

    def test_object_form_does_not_go_through_the_normalizer(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The object form has one reader, the merge, so its bytes cannot drift."""

        def _fail(*_args, **_kwargs):
            raise AssertionError("the object form must not be re-derived from documents")

        monkeypatch.setattr(agent_mod, "normalize_spec_hooks", _fail)
        monkeypatch.setattr(agent_mod, "hook_documents_to_object_form", _fail)
        command = _hook_script(tmp_path)
        hooks = _apply({"preToolUse": [{"matcher": "*", "command": command}]})
        assert hooks == {"preToolUse": [{"command": command, "matcher": "*"}]}

    def test_the_normalizer_does_not_read_the_object_form(self, tmp_path: Path, sel_calls) -> None:
        """One reader owns the object form, so there is no second one to drift.

        Every rule about a command, a matcher, dedup and the caps lives in the
        merge. A normalizer that also read the object form would either restate
        those rules or move where they are applied.
        """
        command = _hook_script(tmp_path)
        assert normalize_spec_hooks({"preToolUse": [{"matcher": "*", "command": command}]}) == []
        assert [c[2] for c in sel_calls] == ["hooks is not an array of hook documents"]

    def test_object_form_bucket_faults_are_audited_by_the_merge(self, sel_calls) -> None:
        assert _apply({"preToolUse": "not-a-list"}) == {}
        assert _apply({"bogusEvent": [{"command": "/bin/true"}]}) == {}
        reasons = [c[2] for c in sel_calls]
        assert "entries not a list" in reasons
        assert "unknown event type" in reasons


class TestANullOptionalReadsAsAbsent:
    """`"matcher": null` is how JSON spells an omitted optional."""

    def _document(self, **extra: object) -> dict:
        document = {
            "name": "h",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": "/bin/true"},
        }
        document.update(extra)
        return document

    @pytest.mark.parametrize("field", ["matcher", "description", "timeout", "enabled", "confirm"])
    def test_a_null_optional_keeps_the_document(self, field: str) -> None:
        (doc,) = normalize_spec_hooks([self._document(**{field: None})])
        assert field not in doc
        assert doc["action"] == {"type": "command", "command": "/bin/true"}

    def test_a_null_matcher_still_reaches_the_kiro_cli_spec(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        document = self._document(matcher=None)
        document["action"] = {"type": "command", "command": command}
        assert _apply([document]) == {"preToolUse": [{"command": command}]}

    def test_a_null_name_is_synthesized(self) -> None:
        (doc,) = normalize_spec_hooks([self._document(name=None)])
        assert doc["name"] == "PreToolUse-0"

    def test_a_null_action_is_still_rejected(self, sel_calls) -> None:
        assert normalize_spec_hooks([self._document(action=None)]) == []
        assert [c[2] for c in sel_calls] == ["action is not an object"]


class TestLessExecutionIsNotDropped:
    """`enabled` and `confirm` grant less execution, so neither is dropped."""

    def _document(self, command: str, **extra: object) -> dict:
        document = {
            "name": "guard",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": command},
        }
        document.update(extra)
        return document

    def test_a_disabled_hook_is_not_emitted(self, tmp_path: Path, caplog, sel_calls) -> None:
        command = _hook_script(tmp_path)
        document = self._document(command, enabled=False)
        assert normalize_spec_hooks([document])[0]["enabled"] is False
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert "disabled" in caplog.text
        assert [c[2] for c in sel_calls] == ["hook is disabled"]
        assert _apply([document]) == {}

    def test_an_enabled_hook_is_emitted(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        assert _apply([self._document(command, enabled=True)]) == {
            "preToolUse": [{"command": command}]
        }

    def test_a_hook_asking_for_confirmation_is_not_emitted(
        self, tmp_path: Path, caplog, sel_calls
    ) -> None:
        command = _hook_script(tmp_path)
        document = self._document(command, confirm=True)
        assert normalize_spec_hooks([document])[0]["confirm"] is True
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert "confirmed" in caplog.text
        assert [c[2] for c in sel_calls] == ["hook asks to be confirmed"]
        assert _apply([document]) == {}

    def test_confirm_false_is_emitted(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        assert _apply([self._document(command, confirm=False)]) == {
            "preToolUse": [{"command": command}]
        }

    def test_an_inexpressible_hook_warns_rather_than_informs(self, caplog, sel_calls) -> None:
        """Audited like every other branch that keeps a configured hook out."""
        document = {
            "name": "later",
            "trigger": "Manual",
            "action": {"type": "agent", "prompt": "summarize"},
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert "no kiro-cli hook" in caplog.text
        assert [c[2] for c in sel_calls] == ["no kiro-cli hook event can express this hook"]

    @pytest.mark.parametrize("trigger", ["PostFileSave", "Manual", "PreTaskExec"])
    def test_an_unmapped_trigger_is_audited_too(self, tmp_path: Path, sel_calls, trigger) -> None:
        command = _hook_script(tmp_path)
        document = {
            "name": "h",
            "trigger": trigger,
            "action": {"type": "command", "command": command},
        }
        assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert [c for c in sel_calls if c[2] == "no kiro-cli hook event can express this hook"]
        assert [c for c in sel_calls if c[0] == trigger]


class TestSharedErrorPath:
    """One defect, two shapes, one rejection."""

    def _object(self, command: str, matcher: str | None = None) -> dict:
        entry: dict = {"command": command}
        if matcher is not None:
            entry["matcher"] = matcher
        return {"preToolUse": [entry]}

    def _array(self, command: str, matcher: str | None = None) -> list[dict]:
        document: dict = {
            "name": "h",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": command},
        }
        if matcher is not None:
            document["matcher"] = matcher
        return [document]

    def test_relative_command_rejected_in_both_shapes(self, sel_calls) -> None:
        assert _apply(self._object("hook.sh")) == {"preToolUse": []}
        object_reasons = [c[2] for c in sel_calls]
        sel_calls.clear()
        assert _apply(self._array("hook.sh")) == {"preToolUse": []}
        assert [c[2] for c in sel_calls] == object_reasons == ["failed validation"]

    def test_missing_command_file_rejected_in_both_shapes(self, tmp_path: Path, sel_calls) -> None:
        missing = str(tmp_path / "absent.sh")
        assert _apply(self._object(missing)) == {"preToolUse": []}
        object_reasons = [c[2] for c in sel_calls]
        sel_calls.clear()
        assert _apply(self._array(missing)) == {"preToolUse": []}
        assert [c[2] for c in sel_calls] == object_reasons == ["failed validation"]

    def test_bad_matcher_rejected_in_both_shapes(self, tmp_path: Path, sel_calls) -> None:
        """Same reason, same dropped hook. The empty bucket is a shape artifact.

        An object-form read keeps the event bucket it was given and drops the
        entry inside it; an array-form read has no bucket to keep, so the event
        does not appear at all. What is pinned is the rejection and its audit
        reason, which are one and the same.
        """
        command = _hook_script(tmp_path)
        assert _apply(self._object(command, matcher="rm -rf /")) == {"preToolUse": []}
        assert [c[2] for c in sel_calls] == ["invalid matcher"]
        sel_calls.clear()
        assert _apply(self._array(command, matcher="rm -rf /")) == {}
        assert [c[2] for c in sel_calls] == ["invalid matcher"]

    def test_caps_apply_to_the_array_form(self, tmp_path: Path) -> None:
        documents = [
            {
                "name": f"h{index}",
                "trigger": "PreToolUse",
                "action": {"type": "command", "command": _hook_script(tmp_path, f"h{index}.sh")},
            }
            for index in range(agent_mod._MAX_USER_HOOKS_PER_EVENT + 4)
        ]
        hooks = _apply(documents)
        assert len(hooks["preToolUse"]) == agent_mod._MAX_USER_HOOKS_PER_EVENT

    def test_dedup_applies_to_the_array_form(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        document = {
            "name": "h",
            "trigger": "PreToolUse",
            "action": {"type": "command", "command": command},
        }
        hooks = _apply([document, dict(document, name="h2")])
        assert hooks == {"preToolUse": [{"command": command}]}


class TestDiagnosticsCarryNoSecret:
    """A rejected value reaches a log line redacted, and SEL whole.

    `gateway.log` persists and rotates rather than expires, and a spec is
    LLM-writable, so a value quoted in a diagnostic can be a credential. The
    audit writer redacts before it truncates, so a caller that pre-slices hands
    the redactors a value already cut at an arbitrary boundary.
    """

    def _document(self, trigger: object) -> dict:
        return {
            "name": "h",
            "trigger": trigger,
            "action": {"type": "command", "command": "/bin/true"},
        }

    def test_a_rejected_trigger_is_redacted_before_it_is_logged(
        self, monkeypatch, sel_calls, caplog
    ) -> None:
        monkeypatch.setattr(agent_mod, "redact", lambda value: "[SCRUBBED]")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert normalize_spec_hooks([self._document("AKIAIOSFODNN7EXAMPLE")]) == []
        assert "[SCRUBBED]" in caplog.text
        assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text

    def test_a_rejected_action_type_is_redacted_before_it_is_logged(
        self, monkeypatch, sel_calls, caplog
    ) -> None:
        monkeypatch.setattr(agent_mod, "redact", lambda value: "[SCRUBBED]")
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "AKIAIOSFODNN7EXAMPLE", "command": "/bin/true"},
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert normalize_spec_hooks([document]) == []
        assert "[SCRUBBED]" in caplog.text
        assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text

    def test_the_audit_value_is_passed_whole(self, sel_calls) -> None:
        """Unsliced: the writer redacts first, so the cut must be its own."""
        trigger = "x" * 500
        assert normalize_spec_hooks([self._document(trigger)]) == []
        _event, value, reason = sel_calls[0]
        assert reason == "unknown trigger"
        assert value == trigger

    def test_an_inexpressible_hook_name_is_redacted(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(agent_mod, "redact", lambda value: "[SCRUBBED]")
        document = {
            "name": "AKIAIOSFODNN7EXAMPLE",
            "trigger": "Manual",
            "action": {"type": "command", "command": "/bin/true"},
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            assert hook_documents_to_object_form(normalize_spec_hooks([document])) == {}
        assert "[SCRUBBED]" in caplog.text
        assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text


class TestArrayFormGuards:
    """Each guard the array form adds, and the input that trips it.

    Every test here fails if its guard is removed: the rejected input would
    become an accepted hook document.
    """

    def test_element_must_be_an_object(self, sel_calls) -> None:
        assert normalize_spec_hooks(["/bin/true"]) == []
        assert [c[2] for c in sel_calls] == ["hook document is not an object"]

    def test_trigger_must_be_known(self, sel_calls) -> None:
        assert (
            normalize_spec_hooks(
                [
                    {
                        "name": "h",
                        "trigger": "WheneverIFeelLikeIt",
                        "action": {"type": "command", "command": "/bin/true"},
                    }
                ]
            )
            == []
        )
        assert [c[2] for c in sel_calls] == ["unknown trigger"]

    def test_trigger_must_be_a_string(self, sel_calls) -> None:
        assert (
            normalize_spec_hooks(
                [{"name": "h", "trigger": 7, "action": {"type": "command", "command": "/bin/true"}}]
            )
            == []
        )
        assert [c[2] for c in sel_calls] == ["unknown trigger"]

    def test_action_must_be_an_object(self, sel_calls) -> None:
        assert normalize_spec_hooks([{"name": "h", "trigger": "Stop", "action": "/bin/true"}]) == []
        assert [c[2] for c in sel_calls] == ["action is not an object"]

    def test_action_type_must_be_known(self, sel_calls) -> None:
        assert (
            normalize_spec_hooks(
                [
                    {
                        "name": "h",
                        "trigger": "Stop",
                        "action": {"type": "eval", "command": "/bin/true"},
                    }
                ]
            )
            == []
        )
        assert [c[2] for c in sel_calls] == ["unknown action type"]

    @pytest.mark.parametrize("command", [None, "", 7, ["/bin/true"]])
    def test_command_action_needs_a_command(self, sel_calls, command: object) -> None:
        action: dict = {"type": "command"}
        if command is not None:
            action["command"] = command
        assert normalize_spec_hooks([{"name": "h", "trigger": "Stop", "action": action}]) == []
        assert [c[2] for c in sel_calls] == ["command action without command"]

    @pytest.mark.parametrize("prompt", [None, "", 7])
    def test_agent_action_needs_a_prompt(self, sel_calls, prompt: object) -> None:
        action: dict = {"type": "agent"}
        if prompt is not None:
            action["prompt"] = prompt
        assert normalize_spec_hooks([{"name": "h", "trigger": "Stop", "action": action}]) == []
        assert [c[2] for c in sel_calls] == ["agent action without prompt"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [("name", 7), ("description", 7), ("enabled", "yes"), ("confirm", "no")],
    )
    def test_optional_fields_are_type_checked(self, sel_calls, field: str, value: object) -> None:
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
            field: value,
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == [f"{field} has the wrong type"]

    def test_empty_name_is_rejected(self, sel_calls) -> None:
        document = {
            "name": "",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == ["name is empty"]

    def test_name_is_synthesized_when_absent(self) -> None:
        (doc,) = normalize_spec_hooks(
            [{"trigger": "Stop", "action": {"type": "command", "command": "/bin/true"}}]
        )
        assert doc["name"] == "Stop-0"

    @pytest.mark.parametrize("timeout", [0, -1, True, "30", 1.5])
    def test_timeout_must_be_a_positive_integer(self, sel_calls, timeout: object) -> None:
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
            "timeout": timeout,
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == ["timeout not a positive integer"]

    @pytest.mark.parametrize("matcher", ["rm -rf /", "a" * 201, 7])
    def test_matcher_follows_the_object_form_rules(self, sel_calls, matcher: object) -> None:
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
            "matcher": matcher,
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == ["invalid matcher"]

    def test_document_count_is_bounded(self, sel_calls, caplog) -> None:
        documents = [
            {
                "name": f"h{i}",
                "trigger": "Stop",
                "action": {"type": "command", "command": "/bin/true"},
            }
            for i in range(_MAX_SPEC_HOOK_DOCUMENTS + 5)
        ]
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            docs = normalize_spec_hooks(documents)
        assert len(docs) == _MAX_SPEC_HOOK_DOCUMENTS
        assert [c[2] for c in sel_calls] == ["document limit exceeded"]

    @pytest.mark.parametrize("action_type", [[], {}, 7, None])
    def test_action_type_must_be_a_string(self, sel_calls, action_type: object) -> None:
        """An unhashable type would raise out of a normalizer that must not raise."""
        action: dict = {"command": "/bin/true"}
        if action_type is not None:
            action["type"] = action_type
        assert normalize_spec_hooks([{"name": "h", "trigger": "Stop", "action": action}]) == []
        assert [c[2] for c in sel_calls] == ["unknown action type"]

    @pytest.mark.parametrize("action_type", ["command", "agent"])
    def test_action_payload_length_is_bounded(self, sel_calls, action_type: str) -> None:
        payload_key = "command" if action_type == "command" else "prompt"
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": action_type, payload_key: "x" * (_MAX_HOOK_PAYLOAD_LEN + 1)},
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == [f"{payload_key} too long"]

    @pytest.mark.parametrize(
        ("field", "limit"),
        [("name", _MAX_HOOK_NAME_LEN), ("description", _MAX_HOOK_DESCRIPTION_LEN)],
    )
    def test_retained_string_fields_are_bounded(self, sel_calls, field: str, limit: int) -> None:
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
            field: "x" * (limit + 1),
        }
        assert normalize_spec_hooks([document]) == []
        assert [c[2] for c in sel_calls] == [f"{field} too long"]

    @pytest.mark.parametrize(
        ("field", "limit"),
        [("name", _MAX_HOOK_NAME_LEN), ("description", _MAX_HOOK_DESCRIPTION_LEN)],
    )
    def test_a_string_field_at_the_limit_is_kept(self, field: str, limit: int) -> None:
        document = {
            "name": "h",
            "trigger": "Stop",
            "action": {"type": "command", "command": "/bin/true"},
            field: "x" * limit,
        }
        (doc,) = normalize_spec_hooks([document])
        assert doc[field] == "x" * limit

    def test_one_bad_document_does_not_drop_the_good_ones(self, tmp_path: Path) -> None:
        command = _hook_script(tmp_path)
        hooks = _apply(
            [
                {
                    "name": "bad",
                    "trigger": "Nope",
                    "action": {"type": "command", "command": command},
                },
                {
                    "name": "good",
                    "trigger": "Stop",
                    "action": {"type": "command", "command": command},
                },
            ]
        )
        assert hooks == {"stop": [{"command": command}]}
