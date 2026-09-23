"""Localized parsing for the second ACP backend's KAS-specific wire surfaces.

Two surfaces live here, and they share one property: the shapes are the second
backend's, not ordinary ACP, so this module is the ONE place Kiro Crew reads or
builds them and an adjustment is a single-file edit.

1. ``session/update`` display/telemetry frames, whose discriminant blob arrives
   in a different shape than ``kiro-cli``'s. The handlers that act on a parsed
   frame live in :mod:`kiro_crew.acp.session_handle`.
2. The **hooks** surface: the backend can delegate hook extraction to its ACP
   client, asking ``_kiro/hooks/list`` for the hooks matching a trigger and
   ``_kiro/hooks/sessionStart`` for results computed before the turn. Kiro Crew
   answers both from its own hook store, so the matcher stays on the side where
   the UI that authored the hook lives.

The literals below are only what Kiro Crew must match to route a frame or answer
a request; the backend's own internals are not documented here.

Read-only by construction. The third method of that surface,
``_kiro/hooks/executeHook``, is the one that spawns a command, and nothing here
implements it: an inbound request naming it is classified as an unknown server
request and answered ``-32601``. What this module does own is the record of
which hook ids Kiro Crew listed for a session (:class:`ListedHookStore`), which
is a NECESSARY condition an execute path has to check and never a sufficient
one: it answers "was this id minted here, for this session", while the hook's
current command and enabled state are the live store's answer, not the
record's.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

# The event vocabulary and the matcher are VALUES: bound directly, because nothing
# substitutes them. The governance helpers are reached through this module instead
# (``hooks_mod.<name>``) -- a local binding cannot be patched at its definition
# site, so a test aiming there would silently get the real governance resolution.
# Same reason the KAS harness reaches its pre-spawn helpers that way.
from kiro_crew import hooks as hooks_mod
from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    _tool_matches,
)

logger = logging.getLogger(__name__)

# ── Envelope keys ──
# Where a frame's discriminant blob sits; both hops are guarded on read.
META_KEY = "_meta"
KIRO_KEY = "kiro"

# ── Frame discriminants Crew matches on ──
KIND_CONTEXT_USAGE = "context_usage"
KIND_TURN_COMPLETION = "turn_completion"
KIND_SUMMARIZATION_STARTED = "summarization_started"
KIND_SUMMARIZATION_COMPLETED = "summarization_completed"
KIND_SUMMARIZATION_FAILED = "summarization_failed"
KIND_STEERING_QUEUED = "steering_queued"
KIND_STEERING_INJECTED = "steering_injected"
KIND_STEERING_CLEARED = "steering_cleared"
KIND_AGENT_SUBTASK = "agent-subtask"  # hyphenated, not underscored

# Summarization maps to Crew's compaction status; steering to mid-turn steer.
SUMMARIZATION_KINDS = frozenset(
    {KIND_SUMMARIZATION_STARTED, KIND_SUMMARIZATION_COMPLETED, KIND_SUMMARIZATION_FAILED}
)
STEERING_KINDS = frozenset(
    {KIND_STEERING_QUEUED, KIND_STEERING_INJECTED, KIND_STEERING_CLEARED}
)

# ── Payload fields Crew reads ──
FIELD_KIND = "kind"
FIELD_USAGE_PERCENTAGE = "usagePercentage"
FIELD_CONVERSATION_SUMMARY = "conversationSummary"
FIELD_CONTENT = "content"
FIELD_PROMPT_TURN_SUMMARIES = "promptTurnSummaries"
FIELD_AGENT_SUBTASK_ID = "agentSubtaskId"
FIELD_PIPELINE = "pipeline"
FIELD_STAGES = "stages"
FIELD_UNIT = "unit"
FIELD_USAGE = "usage"
UNIT_CREDIT = "credit"


def kiro_meta(update: dict) -> dict[str, Any] | None:
    """Return the frame's discriminant blob, or ``None`` when absent.

    Both hops are ``isinstance``-guarded so a frame without it (a different
    backend, or a malformed one) falls through to the shared parser rather than
    raising. The single extraction step every handler shares — do not inline the
    two-step ``.get`` elsewhere.
    """
    meta = update.get(META_KEY)
    kiro = meta.get(KIRO_KEY) if isinstance(meta, dict) else None
    return kiro if isinstance(kiro, dict) else None


def turn_credits(kiro: dict) -> float | None:
    """Sum the per-turn credit cost from a ``turn_completion`` frame.

    Only ``credit``-unit entries contribute (the acp provider bills in credits).
    Returns the total to ASSIGN — the frame carries the whole turn's summary, so
    a replayed/duplicate frame reports the same total and must not accumulate —
    or ``None`` when there is no summaries list, so the caller leaves the prior
    value untouched rather than zeroing it.
    """
    summaries = kiro.get(FIELD_PROMPT_TURN_SUMMARIES)
    if not isinstance(summaries, list):
        return None
    # Deferred import: _token_count lives in the dispatch module, which imports
    # types; importing it at module scope would risk an import cycle once
    # session_handle imports this module.
    from kiro_crew.acp._dispatch import _token_count

    total = 0.0
    for entry in summaries:
        if not isinstance(entry, dict) or entry.get(FIELD_UNIT) != UNIT_CREDIT:
            continue
        value = _token_count(entry.get(FIELD_USAGE))
        if value is not None:
            total += float(value)
    return total


# ── The hooks surface ──
#
# Method names. ``executeHook`` is deliberately NOT named here: a constant for a
# method nothing serves reads as a promise, and the only correct answer to it in
# this build is the unknown-request one every unnamed method already gets.
METHOD_HOOKS_LIST = "_kiro/hooks/list"
METHOD_HOOKS_SESSION_START = "_kiro/hooks/sessionStart"

# The trigger spellings THIS surface uses. They are not the agent-profile
# aliases: ``promptSubmit`` / ``agentStop`` / ``sessionStart`` stand where a
# profile writes ``userPromptSubmit`` / ``stop`` / ``agentSpawn``. Both sets
# normalize on the backend's side, but only these seven are what it asks for
# over this channel, so these seven are what Crew emits.
ACP_TRIGGER_PRE_TOOL_USE = "preToolUse"
ACP_TRIGGER_POST_TOOL_USE = "postToolUse"
ACP_TRIGGER_PROMPT_SUBMIT = "promptSubmit"
ACP_TRIGGER_AGENT_STOP = "agentStop"
ACP_TRIGGER_PRE_TASK_EXECUTION = "preTaskExecution"
ACP_TRIGGER_POST_TASK_EXECUTION = "postTaskExecution"
ACP_TRIGGER_SESSION_START = "sessionStart"

#: Every trigger this surface defines, in the order the covenant lists them.
ACP_HOOK_TRIGGERS: tuple[str, ...] = (
    ACP_TRIGGER_PRE_TOOL_USE,
    ACP_TRIGGER_POST_TOOL_USE,
    ACP_TRIGGER_PROMPT_SUBMIT,
    ACP_TRIGGER_AGENT_STOP,
    ACP_TRIGGER_PRE_TASK_EXECUTION,
    ACP_TRIGGER_POST_TASK_EXECUTION,
    ACP_TRIGGER_SESSION_START,
)

#: Crew event -> the trigger spelling this surface asks for.
#:
#: One key per member of ``HOOK_EVENTS``, and nothing else. The store's event
#: vocabulary is closed on both write paths -- ``validate_hook_fields`` refuses a
#: create outside it, and the loader skips an entry outside it -- so a key for any
#: other spelling could not be reached by a stored hook. A wider trigger
#: vocabulary belongs to the source that can carry it.
#:
#: Two of the seven request triggers therefore have no Crew event:
#: ``preTaskExecution`` and ``postTaskExecution`` are not in ``HOOK_EVENTS``, and
#: a request naming either is answered with an empty list rather than an error.
_CREW_EVENT_TO_ACP_TRIGGER: dict[str, str] = {
    HOOK_EVENT_AGENT_SPAWN: ACP_TRIGGER_SESSION_START,
    HOOK_EVENT_USER_PROMPT_SUBMIT: ACP_TRIGGER_PROMPT_SUBMIT,
    HOOK_EVENT_PRE_TOOL_USE: ACP_TRIGGER_PRE_TOOL_USE,
    HOOK_EVENT_POST_TOOL_USE: ACP_TRIGGER_POST_TOOL_USE,
    HOOK_EVENT_STOP: ACP_TRIGGER_AGENT_STOP,
}

#: The two triggers whose request carries a tool identity, so they are the two
#: where a hook's matcher is a TOOL matcher rather than a context one.
_TOOL_TRIGGERS = frozenset({ACP_TRIGGER_PRE_TOOL_USE, ACP_TRIGGER_POST_TOOL_USE})

#: Cap on one list response, and therefore on the ids one session can hold in
#: :class:`ListedHookStore`. The request is per trigger and per tool call, so an
#: unbounded answer would put the whole hook store on the wire inside a turn. One
#: literal for both, because they describe the same population: a record wider
#: than the answer would hold ids no answer could have produced.
HOOKS_LIST_MAX = 200

#: Bounds on the two host-visible strings a hook carries. A hook past either is
#: WITHHELD rather than truncated: a truncated command is a different command, and
#: a hook whose own text is this far out of range is not one an execute path should
#: be handed. Generous against anything hand-authored -- the store's UI writes a
#: single shell command -- so reaching one means the file was written by something
#: other than a person.
HOOK_COMMAND_MAX = 4096
HOOK_NAME_MAX = 512

#: Bound on the store id, which is the one field :class:`ListedHookStore` RETAINS.
#: The store mints its own as eight hex characters, so anything near this bound is
#: already hand-written; the bound exists because the retained field is the one a
#: bounded map cannot protect by counting entries alone. Withheld like the other
#: two: an id that cannot be trusted to be an id is not one to put on the wire.
HOOK_ID_MAX = 128

#: Id prefix, so an id Crew minted is distinguishable from one the backend
#: derived from a file path in its own loader. Nothing parses the id back: the
#: record in :class:`ListedHookStore` is what an execute path reads.
_ID_PREFIX = "crew"


@dataclass(frozen=True)
class NormalizedHook:
    """One hook, in the shape this surface's reader consumes.

    The reader deliberately does not know how a hook was WRITTEN. Crew's stored
    hooks and an agent spec's hooks are different shapes, and a spec's own shape
    is itself widening; every one of them reaches selection and projection as
    this dataclass, so a new source is one adapter and no change to the three
    steps below it.

    ``event`` keeps Crew's own spelling and :attr:`acp_trigger` resolves it, so
    a hook whose event maps to no trigger on this channel is visibly unserved
    rather than silently renamed.

    The matcher travels; the matcher MODE does not. A mode only matters for a
    matcher evaluated against message context, and this surface never has that
    context -- the request carries a tool identity or nothing at all. So a
    context matcher is not evaluated here, it is a reason to withhold the hook
    (see :func:`select_hooks`), and a field nothing reads would be a field a
    reader has to check.
    """

    id: str
    name: str
    event: str
    command: str = ""
    matcher: str = ""
    timeout: int | None = None
    enabled: bool = True
    #: The source store's own id for this hook. The wire id is derived from it, and
    #: this is what :class:`ListedHookStore` retains so a later read goes back to
    #: the live hook rather than to a copy of its text.
    native_id: str = ""

    @property
    def acp_trigger(self) -> str | None:
        """The trigger spelling this hook answers on, or ``None`` for neither."""
        return _CREW_EVENT_TO_ACP_TRIGGER.get(self.event)


#: The one hook source this surface serves: Crew's script-hook store.
_ID_SOURCE = "script"


def wire_hook_id(native_id: str) -> str:
    """The id Crew puts on the wire for one hook.

    The source segment is a literal rather than a parameter. One source exists, so
    a parameter would be a claim about a second one no caller can make.
    """
    return f"{_ID_PREFIX}:{_ID_SOURCE}:{native_id}"


def normalize_script_hook(hook: Any) -> NormalizedHook | None:
    """Adapt one stored script hook, or ``None`` when it cannot be served.

    ``None`` covers the two cases that are not errors: a hook whose event has no
    trigger on this channel, and a hook with no command. A commandless hook is
    an injection of skill text that the store's own fire path composes, so
    listing it here would promise an action this surface has no shape for.
    """
    event = getattr(hook, "event", "")
    command = getattr(hook, "command", "")
    native_id = getattr(hook, "id", "")
    if not isinstance(event, str) or not isinstance(command, str) or not isinstance(native_id, str):
        return None
    if not native_id or not command.strip():
        return None
    if len(native_id) > HOOK_ID_MAX:
        logger.warning(
            "hook withheld: id is %d chars, past the %d bound",
            len(native_id),
            HOOK_ID_MAX,
        )
        return None
    if event not in _CREW_EVENT_TO_ACP_TRIGGER:
        return None
    if len(command) > HOOK_COMMAND_MAX:
        logger.warning(
            "hook %r withheld: command is %d chars, past the %d bound",
            native_id,
            len(command),
            HOOK_COMMAND_MAX,
        )
        return None
    timeout = getattr(hook, "timeout", None)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        timeout = None
    matcher = getattr(hook, "matcher", "")
    name = getattr(hook, "name", "")
    if isinstance(name, str) and len(name) > HOOK_NAME_MAX:
        logger.warning(
            "hook %r withheld: name is %d chars, past the %d bound",
            native_id,
            len(name),
            HOOK_NAME_MAX,
        )
        return None
    return NormalizedHook(
        id=wire_hook_id(native_id),
        native_id=native_id,
        name=name if isinstance(name, str) and name else native_id,
        event=event,
        command=command,
        matcher=matcher if isinstance(matcher, str) else "",
        timeout=timeout,
        # The literal boolean, not truthiness. The store persists this field
        # uncoerced -- ``ScriptHook.from_dict`` takes ``data.get("enabled", True)``
        # as written -- so a hand-edited ``"enabled": "false"`` arrives as that
        # string, and a truthiness test would read the operator's "off" as "on".
        # Anything that is not ``True`` is treated as off, which is the direction
        # that cannot list a hook its owner switched off.
        enabled=getattr(hook, "enabled", True) is True,
    )


def crew_hooks() -> list[NormalizedHook]:
    """Every Crew hook this surface can serve, normalized.

    The store is the one the dashboard's hook UI writes. When it has not been
    initialized there are no hooks to serve, which answers as an empty list
    rather than an error: an unanswered request would park the agent's turn.
    """
    store = hooks_mod.get_global_hook_store()
    if store is None:
        return []
    out: list[NormalizedHook] = []
    for hook in store.list_all():
        normalized = normalize_script_hook(hook)
        if normalized is not None:
            out.append(normalized)
    return out


def select_hooks(
    hooks: Iterable[NormalizedHook],
    *,
    trigger: str,
    tool_id: str = "",
    include_disabled: bool = False,
) -> list[NormalizedHook]:
    """The hooks that answer one request, in store order.

    Four filters, in the order a caller can reason about:

    * an unrecognized trigger selects nothing. A trigger Crew does not model is
      one it cannot claim a hook matches;
    * the hook's event must resolve to exactly that trigger;
    * a disabled hook is excluded unless the request asked for it. The default
      is the execution-facing one, so a disabled hook is never returned to a
      caller that would act on it;
    * a matcher this request cannot evaluate withholds the hook. On the two tool
      triggers the matcher is a TOOL matcher, so a named tool decides it through
      Crew's own matcher and a request that names none cannot: returning the hook
      there would let it run for a tool its author excluded. On every other
      trigger the matcher is a CONTEXT matcher and the request carries no
      context, so it cannot be decided at all. Withholding is the only direction
      that cannot run a hook against something its matcher rules out; a hook with
      no matcher is unrestricted and is always returned.
    """
    if trigger not in ACP_HOOK_TRIGGERS:
        return []
    out: list[NormalizedHook] = []
    truncated = 0
    for hook in hooks:
        if hook.acp_trigger != trigger:
            continue
        if not hook.enabled and not include_disabled:
            continue
        if hook.matcher:
            if trigger not in _TOOL_TRIGGERS or not tool_id:
                continue
            if not _tool_matches(hook.matcher, tool_id):
                continue
        if len(out) >= HOOKS_LIST_MAX:
            truncated += 1
            continue
        out.append(hook)
    if truncated:
        # Said out loud: a silent cut reads downstream as "the user has no more
        # hooks for this trigger", which is a different fact.
        logger.warning(
            "hooks list for trigger %r truncated at %d; %d further hook(s) withheld",
            trigger,
            HOOKS_LIST_MAX,
            truncated,
        )
    return out


def project_hook(hook: NormalizedHook) -> dict[str, Any]:
    """One hook in the wire shape: ``{id, name, action}``.

    ``approved`` is never sent. It is the field that tells the agent this
    command already carries an approval, and Crew has no approval to report for
    a hook it has not been asked to run.
    """
    action: dict[str, Any] = {"type": "runCommand", "command": hook.command}
    if hook.timeout is not None:
        action["timeout"] = hook.timeout
    return {"id": hook.id, "name": hook.name, "action": action}


def _params_object(params: Any) -> Mapping[str, Any]:
    """The request's params as a mapping, or an empty one.

    Guarded HERE and not only at the route, because the route is one caller and
    a params object that is absent, null or a scalar is a shape the host can send
    on any of them. An answer built from an empty mapping is the same answer as
    one built from an empty params object, which is what makes this safe to
    degrade rather than raise inside a dispatch loop.
    """
    return params if isinstance(params, Mapping) else {}


def _str_param(params: Mapping[str, Any], key: str) -> str:
    """One string field of a host-supplied params object, or ``""``."""
    value = params.get(key)
    return value if isinstance(value, str) else ""


def script_hooks_forbidden(session_id: str = "") -> str | None:
    """The governance denial that forbids script hooks, or ``None``.

    The same gate the store's own fire path consults, asked here because listing
    is the step that makes a hook reachable: a policy that forbids script hooks
    must not be handed the set either.

    The QUESTION is asked on an empty key, which is the capability gate's own
    ``enabled`` flag. An ACP session handle holds no Crew session key -- its
    constructor is given a backend session id and a Crew agent, and its own SEL
    audits pass ``session_key=""`` for that reason -- so a key here would be
    fabricated, and a fabricated one resolves no profile while LOOKING like an
    identity. What that costs is stated rather than hidden: a denial scoped to a
    Crew session is not reachable from this surface, and the global flag is.

    The DECISION is audited either way, under the ACP session as its correlation
    key, so a listed set and a refused one both leave a record naming which
    session asked.

    A composition failure is treated as a denial rather than propagated: the
    store's fail-closed CPP rule is the same judgement, and an exception raised
    here would leave the request unanswered instead of answered with nothing.
    """
    audit_key = f"acp:{session_id}" if session_id else ""
    try:
        denial = hooks_mod._script_hooks_capability_denied("")
    except Exception:
        logger.warning(
            "hooks list: governance could not be resolved; answering none", exc_info=True
        )
        hooks_mod._audit_governance_hook_decision(
            audit_key, "kas_hooks_list", "denied", "governance unresolved"
        )
        return "governance unresolved"
    hooks_mod._audit_governance_hook_decision(
        audit_key,
        "kas_hooks_list",
        "denied" if denial is not None else "allowed",
        denial if denial is not None else "script_hooks capability permitted",
    )
    return denial


def hooks_list_response(
    params: Any,
    hooks: Iterable[NormalizedHook] | None = None,
    *,
    session_id: str = "",
) -> dict[str, Any]:
    """Answer ``_kiro/hooks/list``.

    Every field of ``params`` is host-supplied, so each is read through an
    isinstance guard and a shape that is not the expected one degrades to
    absent. ``toolTags`` and ``workspacePaths`` are read and deliberately
    ignored: Crew's hooks carry no tag matcher, and they are not scoped to a
    workspace root, so narrowing by either would drop hooks their author expects
    to fire.

    Records what it answered against ``session_id``, which the CALLER supplies
    from the session it owns -- never the ``sessionId`` in ``params``. The record
    is the necessary condition an execute path checks, so letting the request name
    its own key would let one session's request write another session's set. A
    caller with no session id records nothing rather than recording under a blank
    key.

    Answers with no hooks when governance forbids script hooks. The empty list is
    the only shape this method has for that, and the reason is logged, because the
    agent's own policy is to read a failed call as "no hooks" -- so an error
    response would be indistinguishable from this and would lose the reason too.
    """
    params = _params_object(params)
    trigger = _str_param(params, "trigger")
    tool_id = _str_param(params, "toolId")
    include_disabled = params.get("includeDisabled") is True
    denial = script_hooks_forbidden(session_id)
    if denial is not None:
        logger.warning("hooks list refused for trigger %r: %s", trigger, denial)
        return {"hooks": []}
    selected = select_hooks(
        crew_hooks() if hooks is None else hooks,
        trigger=trigger,
        tool_id=tool_id,
        include_disabled=include_disabled,
    )
    if session_id:
        listed_hooks().record(session_id, selected)
    return {"hooks": [project_hook(hook) for hook in selected]}


def hooks_session_start_response(params: Any) -> dict[str, Any]:
    """Answer ``_kiro/hooks/sessionStart`` with the results buffered for a turn.

    Empty for every trigger, which is the honest answer for this build rather
    than a stub: a precomputed result is the OUTPUT of a hook, every Crew hook
    this surface can serve carries a command as its payload, and no command runs
    from here. The request is still ANSWERED, because an unanswered one parks
    the agent's turn waiting for a response that never arrives, and the agent's
    own policy for this surface is to treat a failure as "no hooks" — which is
    indistinguishable from a refusal and is the reason a refusal must never be
    expressed that way.

    ``params`` is accepted and not read: there is no field whose value could
    make the answer non-empty.
    """
    return {"results": []}


class ListedHookStore:
    """Which hook ids Crew listed as runnable, per session, and what each points at.

    The point of the record is the direction it can answer in the negative. An
    execute path may only run a hook Crew itself put on the wire for THAT
    session, and the id the agent sends back is host-supplied, so "is this id one
    of mine" has to be a lookup rather than a parse of the id's shape.

    A hit is a NECESSARY condition and never a sufficient one. What it answers is
    "this wire id was minted here, for this session, for a hook that was runnable",
    and what it hands back is the STORE's own id for that hook -- so an execute path
    re-reads the hook it is about to run from the live store, which owns both the
    command and the `enabled` state. A user can edit or switch off a hook between
    the two calls, and the record deliberately cannot tell you otherwise.

    Nothing the hook CARRIES is retained: no command, no name. Those are the two
    unbounded strings on a hook, they are already bounded for the wire by
    :data:`HOOK_COMMAND_MAX` and :data:`HOOK_NAME_MAX`, and keeping a second copy
    resident across a bounded-but-long-lived map would be a third bound to get
    right for no gain. The store's id is its own 8-character handle.

    Bounded on both axes, because both are driven by the host: sessions are
    evicted oldest-first, and ids within a session likewise. Eviction can only
    ever narrow what is executable, so the bound fails in the safe direction.
    """

    def __init__(
        self, *, max_session_records: int = 64, max_per_session: int = HOOKS_LIST_MAX
    ) -> None:
        self._max_session_records = max_session_records
        self._max_per_session = max_per_session
        self._lock = threading.Lock()
        self._by_session: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()

    def record(self, session_id: str, hooks: Sequence[NormalizedHook]) -> None:
        """Record the RUNNABLE hooks one list response returned for ``session_id``.

        A disabled hook is skipped even when the request asked for it. That
        request is the one a tree view makes, its answer is display data, and
        recording it would leave an id the user switched off indistinguishable
        from one they left on.
        """
        if not session_id:
            return
        with self._lock:
            listed = self._by_session.pop(session_id, None)
            if listed is None:
                listed = OrderedDict()
            for hook in hooks:
                if not hook.enabled:
                    continue
                listed.pop(hook.id, None)
                listed[hook.id] = hook.native_id
                while len(listed) > self._max_per_session:
                    listed.popitem(last=False)
            self._by_session[session_id] = listed
            while len(self._by_session) > self._max_session_records:
                self._by_session.popitem(last=False)

    def native_id_for(self, session_id: str, hook_id: str) -> str | None:
        """The store id behind a listed wire id, or ``None`` when it was never listed.

        ``None`` is the refusal an execute path acts on. A string is the handle it
        re-reads the live hook with; it is not permission to run what the request
        said.
        """
        with self._lock:
            listed = self._by_session.get(session_id)
            return listed.get(hook_id) if listed is not None else None

    def forget(self, session_id: str) -> None:
        """Drop one session's record, on the session going away."""
        with self._lock:
            self._by_session.pop(session_id, None)


_LISTED_HOOKS = ListedHookStore()


def listed_hooks() -> ListedHookStore:
    """The process-wide record of what each session was told."""
    return _LISTED_HOOKS
