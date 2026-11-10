"""action.py — the action subsystem.

Actions are GM-defined effect bundles stored under entity vars at any
path ending in `<container>.actions.<name>`. The full vars subtree at
that name is an Action dict:

    {
        "body": "<formula program with cmd()/source/fail extensions>",
        "description": "<human-readable explanation>",   # optional
        "target":      "entity|location|entity_list|location_list|none",
                                                          # optional, default "none"
    }

Discovery walks the entity's whole vars tree (subject to
action_container_mode / action_container_paths) and surfaces every
such dict as an available action. The body is run when the player
issues `!action <eid> <name> [target] [k=v ...]` (or the equivalent
`!ent <eid> action <name> ...`). The runner is transactional: it
captures pre-state, suspends the dispatcher's per-command snapshots
for the duration of the body (so a single action is one undo unit),
runs the body, and on any failure rolls back to the captured state.

Three runtime concepts the body language adds on top of plain
formulas:

  cmd(line)         dispatch a `!command` from inside an action,
                    subject to action_cmd_allowlist
  fail(message)     abort the body cleanly; the runner rolls back,
                    surfaces `❌ <action>: <message>`, and does NOT
                    fire on_action_used (unlike a successful body)
  source            a proxy bound to the enclosing container; reads
                    and writes pass through to `entity.vars.<container>`.
                    For a top-level action (no enclosing container)
                    `source` aliases the entity's vars root, so
                    `source.<path>` is equivalent to
                    `entity[self].<path>`. Reserved name — cannot be
                    used as a vars key.

  target            the resolved target token(s) — shape depends on
                    the action's declared target type
  args              a dict view over the GM-supplied key=value tokens

The implementation lives here (data shape + proxies + runner) rather
than in logic.py so the domain model stays browsable in one file. The
formula sandbox (formula.py) gains the new builtins; the command
surface (vtt_commands.py) gains `!action`.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import copy
import re
import contextvars
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from logic import (VTTError, NotFound, same_value, CROSS_MATCH_GUARD, VAR_READER, normalize_tags, caller_capabilities, MATCH_CAPABILITIES,
                   ACTIONS_USED_MAX,
                   check_tag_filter, tag_filter_matches, tag_filter_empty,
                   RULES_REGISTRY)

# The other-matches guards of the action levels running now, outermost
# first (each a run_action's `cross`): a level that changes another match
# saves it for every level above that hasn't yet.
_CROSS_LEVELS: "contextvars.ContextVar[tuple]" = contextvars.ContextVar(
    "vtt_cross_levels", default=())

if TYPE_CHECKING:
    from logic import Match, Entity


class _BufferCtx:
    """Synchronous reply sink used for the command dispatch an action
    body performs via cmd() (and for nested sub-actions).

    The crux: the formula engine is SYNCHRONOUS, but command handlers
    are async and a real reply context (e.g. the Discord bot) awaits a
    network round-trip inside send(). A synchronous formula can't await
    that. _BufferCtx.send is a coroutine that completes WITHOUT a real
    suspension point (it just appends to a list), so the sync engine
    can drive a dispatched command to completion by pumping the
    coroutine (see _sync_dispatch) — there's no pending future to wait
    on. The buffered lines are flushed through the REAL awaitable
    reply context after the top-level action finishes, back in async
    land where awaiting is legal.

    channel_key mirrors the real context so active-match lookups inside
    the dispatched command resolve to the same match."""

    def __init__(self, channel_key: str, user_id: Optional[str] = None,
                 user_name: Optional[str] = None):
        self.channel_key = channel_key
        # Who ran the action: every identity check a cmd() line meets
        # (a transfer into another match needs its host) sees that user. An
        # identity-less context passed those checks (the gate's no-op for
        # single-operator surfaces), so a cmd() line reached any match.
        # Never an administrator (`is_admin` unset: default-deny).
        self.user_id = user_id
        self.user_name = user_name
        self.out: List[str] = []

    @classmethod
    def for_ctx(cls, ctx: Any) -> "_BufferCtx":
        out = cls(getattr(ctx, "channel_key", "CLI"),
                  getattr(ctx, "user_id", None),
                  getattr(ctx, "user_name", None))
        # The runner's per-match capabilities (#190: `gm` hosts every
        # match, `templates`...) carry over like its identity; the
        # server-wide ones don't, as the administrator flag doesn't.
        out.capabilities = sorted(caller_capabilities(ctx)
                                  & MATCH_CAPABILITIES)
        return out

    async def send(self, message: str) -> None:
        self.out.append(message)


# Reserved subkey under any vars container that holds the action
# dictionary. A container with a key named "actions" whose value is a
# dict has each child of that dict treated as an Action definition.
ACTIONS_KEY = "actions"

# Action tags (#234). An action's own `tags` stay with it; a container's
# `action_tags` (any dict above an action: the vars root, an item, an
# `actions` dict, a mount slot) reach every action below it; an
# `exclude_inherited_action_tags` (`all` / `*`, or tags) on an action or a
# container cuts the tags arriving from above, for everything at and below
# it. Statuses forbid or block actions by their final tags (logic.py,
# Match.action_restrictions).
ACTION_OWN_TAGS_KEY = "tags"
# Action fields filtering an entity / corpse target by the target's tags.
TARGET_TAG_FIELDS = ("target_requires_tags", "target_refuses_tags")
ACTION_TAGS_KEY = "action_tags"
ACTION_TAG_EXCLUDE_KEY = "exclude_inherited_action_tags"
# Keys of an `actions` dict read as the dict's own tag fields, never as
# actions.
_ACTIONS_DICT_FIELDS = (ACTION_TAGS_KEY, ACTION_TAG_EXCLUDE_KEY)

# Allowed values for an Action dict's `target` field. Anything else at
# definition time is a validation error.
ALLOWED_TARGET_TYPES = frozenset({
    "entity", "location", "entity_list", "location_list", "none",
    # Corpse targets: the input is a corpse id (string). The runner
    # binds `target` to that id; the body uses it via revive(target)
    # / has_corpse(target) / corpse_at(target). `corpse_list` accepts
    # multiple ids (loopable in the body).
    "corpse", "corpse_list",
})

# Reserved bindings the action body language injects into the eval
# namespace. The GM cannot use these as vars keys at the affected paths
# (the validator + the cmd dispatcher refuse them).
ACTION_RESERVED_BINDINGS = frozenset({
    "source", "target", "args", "cmd", "fail",
})


class ActionFail(Exception):
    """Raised by fail(...) inside an action body to abort the body
    cleanly. The runner catches this, rolls back to pre-state, and
    replies `❌ <action>: [<reason>] <message>` (reason is dropped
    from the prefix when empty). NOT a FormulaError — that would be
    caught and re-wrapped by the formula engine; ActionFail bubbles
    past the engine's catch so the runner can see it.

    `reason` is a free-form GM-chosen tag — "cost", "out_of_range",
    "invalid_target", etc. — surfaced to on_action_failed passives
    via the `fail_reason` binding. Empty string means "no reason
    tag" (the single-arg `fail("msg")` form)."""

    def __init__(self, message: str, *, reason: str = "",
                 refund: bool = False):
        super().__init__(message)
        self.message = message
        self.reason = reason
        # fail(refund) (#36): a body failing after reactions ran rolls the
        # whole exchange back, the reactions' effects too.
        self.refund = refund


class _RefundWord(str):
    """The word `refund` bound in action bodies, which fail() reads."""


REFUND = _RefundWord("refund")


class ActionValidationError(VTTError):
    """Raised when an action dict's shape (or body's syntax) is
    invalid at definition time. Subclass of VTTError so the command
    dispatcher's standard `❌ <error>` path catches it."""
    pass


class ChoiceNeeded(Exception):
    """Raised by the choose() / choose_number() bindings when an action
    body needs an interactive pick that hasn't been pre-supplied. The
    TOP-LEVEL run_action catches it, rolls back the attempt, obtains an
    answer (interactively, or fails cleanly if the surface can't prompt),
    appends it to the answer queue, and re-runs the body from the top —
    the replay model. Not an ActionFail (the action isn't aborting) and
    not a FormulaError (the formula engine must let it pass through to the
    runner — it's in eval_program's unwrapped-exception set).

    `options` is the candidate list for choose(); None for choose_number(),
    which carries an integer range in `lo`/`hi` instead."""
    def __init__(self, prompt: str, options: Optional[List[Any]] = None, *,
                 lo: Optional[int] = None, hi: Optional[int] = None):
        super().__init__(prompt)
        self.prompt = prompt
        self.options = options
        self.lo = lo
        self.hi = hi


# Answer tokens (any case) that mean "abort this action" at a prompt.
CHOICE_CANCEL_TOKENS = frozenset({"cancel", "__cancel__"})

# Sentinel for "the supplied answer matched no option".
_CHOICE_MISS = object()


def _match_option(raw: Any, options: List[Any]) -> Any:
    """Match a supplied answer against an option list — exact value first,
    then string-equality (so the CLI/Discord token "3" matches the int 3,
    "fire" matches "fire"). Returns the matched OPTION (preserving its
    original type) or _CHOICE_MISS."""
    for opt in options:
        if raw == opt or str(raw) == str(opt):
            return opt
    return _CHOICE_MISS


def _snapshot_rng(match: "Match") -> Dict[str, Any]:
    """Capture RNG state so the body replays the same random draws across
    choice prompts (a roll BEFORE a choice stays stable). Covers both the
    global RNG (unseeded) and the match-seeded RNG."""
    import random
    state: Dict[str, Any] = {"global": random.getstate()}
    # Build the seeded RNG now if a seed is set, so a body that makes the
    # first roll of the match still replays it identically.
    rng = match.formula_rng() if hasattr(match, "formula_rng") else None
    if rng is random:
        rng = None
    if rng is not None:
        state["match"] = rng.getstate()
    return state


def _restore_rng(match: "Match", state: Dict[str, Any]) -> None:
    import random
    random.setstate(state["global"])
    rng = getattr(match, "_rng", None)
    if rng is not None and "match" in state:
        rng.setstate(state["match"])


# Answers given at an interactive prompt during the command running now, in
# order: the command journal replays them as `answer=` tokens.
INTERACTIVE_ANSWERS: "contextvars.ContextVar[Optional[list]]" = \
    contextvars.ContextVar("vtt_interactive_answers", default=None)


async def _obtain_answer(ctx: Any, nc: "ChoiceNeeded") -> Any:
    """Get one interactive answer for a ChoiceNeeded. Uses the surface's
    `prompt_choice(prompt, options, lo, hi)` coroutine if it has one (the
    CLI); otherwise there's no way to ask, so raise ActionFail telling the
    GM to pre-supply `answer=` tokens (the harness / a menu-less surface
    path). Re-prompts on an invalid pick; a None / "cancel" reply aborts
    the action (ActionFail). Returns a value choose()/choose_number() will
    accept on replay."""
    prompter = getattr(ctx, "prompt_choice", None)
    if not callable(prompter):
        raise ActionFail(
            f"this action needs a choice ('{nc.prompt}') but no answer was "
            f"supplied — pass it as an `answer=<value>` token, or run on a "
            f"surface that can prompt.",
            reason="needs_choice",
        )
    while True:
        raw = await prompter(nc.prompt, nc.options, nc.lo, nc.hi)
        if raw is None or (isinstance(raw, str)
                           and raw.strip().lower() in CHOICE_CANCEL_TOKENS):
            raise ActionFail("choice cancelled.", reason="cancelled")
        if nc.options is not None:
            if _match_option(raw, nc.options) is not _CHOICE_MISS:
                return raw
        else:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                continue
            if nc.lo <= n <= nc.hi:
                return n
        # invalid -> loop and ask again


class ActionEngineFault(Exception):
    """Engine-level refusal to keep running an action chain — the
    recursion limit was hit, an internal invariant broke, etc.
    DISTINCT from ActionFail (which is the GM's clean abort signal):
    engine faults propagate up the action chain regardless of
    use_action() return-value branching, because the engine is
    refusing the operation rather than the GM's body choosing to
    bail. use_action() catches ActionFail and translates to a False
    return; ActionEngineFault is allowed to bubble past."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ----------------------------------------------------------------------
# Reaction fields (#36)
# ----------------------------------------------------------------------

REACTS_ON = ("target", "any")
AFTER_ON = ("resolved", "cancelled", "any")
ON_CANCEL = ("refund", "keep")
CANCEL_COST = ("refund", "pay")


@dataclass
class ReactionSpec:
    """What makes an action a reaction (docs/damage_reactions_design.md
    §2): its halves, what it answers, when it is available and how it
    orders. `cancels` is detected: the before half assigns
    `incoming.cancel` somewhere in its text."""
    before: str = ""
    after: str = ""
    reacts_to: Any = None          # a checked tag filter, or None = any
    reacts_to_error: str = ""      # a filter that doesn't parse: matches nothing
    reacts_on: str = "target"
    condition: str = ""
    after_on: str = "resolved"
    priority: float = 0
    before_priority: Optional[float] = None
    after_priority: Optional[float] = None
    self_trigger: bool = False
    on_cancel: Optional[str] = None   # reaction_on_cancel override
    auto_when: str = ""
    cancels: bool = False
    # A bystander reaction without a condition is never offered.
    disabled: str = ""


def detects_cancel(src: str) -> bool:
    """Whether the program `src` assigns `incoming.cancel` (or
    `incoming_of(...).cancel`) anywhere, a branch included."""
    import ast
    try:
        tree = ast.parse(src or "", mode="exec")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if not (isinstance(t, ast.Attribute) and t.attr == "cancel"):
                continue
            v = t.value
            if isinstance(v, ast.Name) and v.id == "incoming":
                return True
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                    and v.func.id == "incoming_of"):
                return True
    return False


def _formula_field(raw: Dict[str, Any], key: str, full_path: str,
                   problems: List[str]) -> str:
    """A formula-text field of an action dict (normalized), "" when
    absent; a value that isn't text is named in `problems` and ignored."""
    from formula import normalize_body_source
    v = raw.get(key)
    if v is None or v == "":
        return ""
    if not isinstance(v, str):
        problems.append(f"`{full_path}.{key}` isn't formula text "
                        f"({type(v).__name__}); it is ignored.")
        return ""
    return normalize_body_source(v)


def _expression_field(raw: Dict[str, Any], key: str, full_path: str,
                      problems: List[str]) -> str:
    """An expression field that may also hold a bool: `!ent set_var u
    actions.r.condition true` stores True, which reads as always."""
    v = raw.get(key)
    if isinstance(v, bool):
        return "True" if v else "False"
    return _formula_field(raw, key, full_path, problems)


def _word_field(raw: Dict[str, Any], key: str, choices: Tuple[str, ...],
                default: Optional[str], full_path: str,
                problems: List[str]) -> Optional[str]:
    v = raw.get(key)
    if v is None:
        return default
    w = str(v).strip().lower()
    if w not in choices:
        problems.append(f"`{full_path}.{key}` must be one of "
                        f"{', '.join(choices)}, not {v!r}; "
                        f"{default or 'the rule'} applies.")
        return default
    return w


def _number_field(raw: Dict[str, Any], key: str, full_path: str,
                  problems: List[str]) -> Optional[float]:
    import math
    v = raw.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(v):
        try:
            v = float(str(v))
            if not math.isfinite(v):
                raise ValueError
        except ValueError:
            problems.append(f"`{full_path}.{key}` must be a number, "
                            f"not {raw.get(key)!r}; it is ignored.")
            return None
    return int(v) if float(v).is_integer() else float(v)


def _parse_reaction(raw: Dict[str, Any], full_path: str,
                    problems: List[str]) -> Optional[ReactionSpec]:
    """The reaction fields of an action dict, or None for an action that
    has neither half."""
    if raw.get("before") is None and raw.get("after") is None:
        return None
    spec = ReactionSpec()
    spec.before = _formula_field(raw, "before", full_path, problems)
    spec.after = _formula_field(raw, "after", full_path, problems)
    rt = raw.get("reacts_to")
    if rt is not None and not tag_filter_empty(rt):
        try:
            spec.reacts_to = check_tag_filter(rt)
        except VTTError as ex:
            spec.reacts_to = rt
            spec.reacts_to_error = str(ex)
            problems.append(f"`{full_path}.reacts_to`: {ex} It matches "
                            f"nothing until fixed.")
    spec.reacts_on = _word_field(raw, "reacts_on", REACTS_ON, "target",
                                 full_path, problems) or "target"
    spec.condition = _expression_field(raw, "condition", full_path,
                                       problems)
    spec.after_on = _word_field(raw, "after_on", AFTER_ON, "resolved",
                                full_path, problems) or "resolved"
    spec.priority = _number_field(raw, "priority", full_path, problems) or 0
    spec.before_priority = _number_field(raw, "before_priority", full_path,
                                         problems)
    spec.after_priority = _number_field(raw, "after_priority", full_path,
                                        problems)
    st = raw.get("self_trigger")
    if isinstance(st, str):
        st = st.strip().lower() in ("true", "yes", "on", "1")
    spec.self_trigger = bool(st)
    spec.on_cancel = _word_field(raw, "reaction_on_cancel", ON_CANCEL, None,
                                 full_path, problems)
    spec.auto_when = _expression_field(raw, "auto_when", full_path,
                                       problems)
    spec.cancels = bool(spec.before) and detects_cancel(spec.before)
    if not spec.before and not spec.after:
        spec.disabled = "it has no `before` or `after` text"
    elif spec.reacts_on == "any" and not spec.condition:
        spec.disabled = ("a `reacts_on: any` reaction needs a `condition` "
                         "(`condition: true` answers every action)")
    if spec.disabled:
        problems.append(f"`{full_path}` is never offered: {spec.disabled}.")
    return spec


# ----------------------------------------------------------------------
# Action dataclass
# ----------------------------------------------------------------------

@dataclass
class Action:
    """One discovered action ready to be invoked.

    `name`            the bare action name (e.g. "slice")
    `body`            the formula program source (string)
    `description`     GM-facing prose; "" if not declared
    `target_type`     one of ALLOWED_TARGET_TYPES; "none" if not declared
    `container_path`  the dotted vars path of the ENCLOSING container —
                      what `source` binds to. Empty string for a
                      top-level action (lives under `entity.vars.actions
                      .<name>` directly). Example: an action at
                      `inventory.sword.actions.slice` has
                      container_path = "inventory.sword".
    `full_path`       the dotted vars path of this action's dict —
                      e.g. "inventory.sword.actions.slice". Used as the
                      stable identifier for disambiguation menus and
                      as the `action_path` binding fed to
                      on_action_used.
    """
    name: str
    body: str
    description: str
    target_type: str
    container_path: str
    full_path: str
    # Mounts: when this action is offered to a vehicle's RIDERS, the slot
    # names allowed to use it (parsed from the def's `allowed_slots` —
    # list or CSV; "*"/"all" = any slot). None = not a rider-shared
    # action (the vehicle's own). Only consulted for vehicle-owned actions
    # during mount-action discovery; ignored for ordinary entity actions.
    allowed_slots: Optional[List[str]] = None
    # The action's final tags: its own `tags`, then the inherited ones
    # nearest first (what survived the exclusions), each once.
    tags: List[str] = field(default_factory=list)
    # tag -> where it came from: the action's own path, a container's path
    # ("" = the vars root).
    tag_sources: Dict[str, str] = field(default_factory=dict)
    # Tag fields that weren't a tag list (a value dropped from discovery's
    # view, named by `!action info`).
    tag_problems: List[str] = field(default_factory=list)
    # Tag filters an entity / corpse target must match (requires) and must
    # not match (refuses); None = no such field. A filter that doesn't
    # parse is kept as written and refuses every target, naming the error.
    target_requires_tags: Any = None
    target_refuses_tags: Any = None
    # The unit whose vars hold the action (set by discover_actions; a
    # vehicle for a rider's seat action). run_action records a successful
    # use on it, per team that saw it (Entity.actions_seen).
    holder: Optional[str] = None
    # The line printed after a successful run (#215): placeholders read the
    # body's locals, `self` / `target` (names as each view sees the unit,
    # `.name` / `.<var path>`) and `args.<key>`. "" = none.
    say_format: str = ""
    # Reactions (#36, docs/damage_reactions_design.md): `prepare` runs when
    # the action is used, before any reaction window, its locals becoming
    # `incoming`; `cost` runs right before the body (a cancelled action's
    # per cancelled_action_cost); `affects` names the units the action
    # directly affects (default its entity targets); `target_condition`
    # judges each target when the action is used and again at resolution.
    prepare: str = ""
    cost: str = ""
    affects: str = ""
    target_condition: str = ""
    cancelled_action_cost: Optional[str] = None
    # Set when the action is a reaction (it holds a `before` and / or
    # `after` half).
    reaction: Optional["ReactionSpec"] = None

    @staticmethod
    def from_dict(
        name: str, raw: Any, *,
        container_path: str, full_path: str,
        inherited: Optional[List[Tuple[str, str]]] = None,
        inherited_problems: Optional[List[str]] = None,
    ) -> "Action":
        """Build an Action from a vars subtree. Raises
        ActionValidationError if the shape is wrong."""
        if not isinstance(raw, dict):
            raise ActionValidationError(
                f"action '{name}' at `{full_path}` must be a dict "
                f"(got {type(raw).__name__})."
            )
        body = raw.get("body", "")
        is_reaction = (raw.get("before") is not None
                       or raw.get("after") is not None)
        if is_reaction and body in (None, ""):
            body = ""
        elif not isinstance(body, str) or not body.strip():
            raise ActionValidationError(
                f"action '{name}' at `{full_path}`: `body` is "
                f"required and must be a non-empty string."
            )
        # Translate the documentation escapes `\n` and `\t` so a body
        # typed at the CLI / Discord (where shlex preserves literal
        # backslash-n) loads with real newlines for ast.parse. See
        # formula.normalize_body_source for the full rationale.
        from formula import normalize_body_source
        body = normalize_body_source(body)
        description = raw.get("description", "")
        if not isinstance(description, str):
            raise ActionValidationError(
                f"action '{name}' at `{full_path}`: `description` "
                f"must be a string."
            )
        say_format = raw.get("say_format", "")
        if say_format is None:
            say_format = ""
        if not isinstance(say_format, str):
            raise ActionValidationError(
                f"action '{name}' at `{full_path}`: `say_format` "
                f"must be a string."
            )
        target_type = raw.get("target", "none")
        if not isinstance(target_type, str) or target_type not in ALLOWED_TARGET_TYPES:
            allowed = ", ".join(sorted(ALLOWED_TARGET_TYPES))
            raise ActionValidationError(
                f"action '{name}' at `{full_path}`: `target` must be "
                f"one of {{{allowed}}}; got {target_type!r}."
            )
        raw_slots = raw.get("allowed_slots")
        allowed_slots: Optional[List[str]] = None
        if isinstance(raw_slots, str):
            allowed_slots = _split_csv(raw_slots)
        elif isinstance(raw_slots, (list, tuple)):
            allowed_slots = [str(s).strip() for s in raw_slots if str(s).strip()]
        problems: List[str] = list(inherited_problems or [])
        chain = _inherit_tags(inherited or [], raw, full_path, problems,
                              own_key=None)
        own = _tag_field(raw, ACTION_OWN_TAGS_KEY, full_path, problems)
        tags: List[str] = []
        sources: Dict[str, str] = {}
        # Own tags first, then each container's nearest first, in the
        # order the container lists them.
        order = list(dict.fromkeys(src for _t, src in chain))
        nearest = [(t, src) for o in reversed(order)
                   for t, src in chain if src == o]
        for t, src in [(t, full_path) for t in own] + nearest:
            if t not in sources:
                tags.append(t)
                sources[t] = src
        filters: Dict[str, Any] = {}
        for key in TARGET_TAG_FIELDS:
            raw_f = raw.get(key)
            if raw_f is None or tag_filter_empty(raw_f):
                continue
            try:
                filters[key] = check_tag_filter(raw_f)
            except VTTError as ex:
                problems.append(f"`{full_path}.{key}`: {ex}")
                filters[key] = raw_f
        prepare = _formula_field(raw, "prepare", full_path, problems)
        cost = _formula_field(raw, "cost", full_path, problems)
        affects = _formula_field(raw, "affects", full_path, problems)
        target_condition = _formula_field(raw, "target_condition",
                                          full_path, problems)
        cancel_cost = _word_field(raw, "cancelled_action_cost", CANCEL_COST,
                                  None, full_path, problems)
        reaction = _parse_reaction(raw, full_path, problems)
        return Action(
            name=name, body=body, description=description,
            target_type=target_type,
            container_path=container_path, full_path=full_path,
            allowed_slots=allowed_slots,
            tags=tags, tag_sources=sources, tag_problems=problems,
            target_requires_tags=filters.get("target_requires_tags"),
            target_refuses_tags=filters.get("target_refuses_tags"),
            say_format=normalize_body_source(say_format),
            prepare=prepare, cost=cost, affects=affects,
            target_condition=target_condition,
            cancelled_action_cost=cancel_cost, reaction=reaction,
        )


def _tag_field(d: Dict[str, Any], key: str, path: str,
               problems: List[str]) -> List[str]:
    """The tags `d[key]` holds (a list or comma / space text), noting in
    `problems` a value that isn't a tag list or words that can't be tags."""
    raw = d.get(key)
    if raw is None:
        return []
    tags = normalize_tags(raw)
    if not isinstance(raw, (str, list)) or (isinstance(raw, list) and any(
            not isinstance(t, str) or t.strip() not in tags for t in raw)):
        where = f"{path}.{key}" if path else key
        problems.append(f"`{where}` isn't a list of one-word tags "
                        f"({raw!r}); only {tags or 'nothing'} counts.")
    return tags


def _inherit_tags(chain: List[Tuple[str, str]], d: Dict[str, Any],
                  path: str, problems: List[str],
                  own_key: Optional[str] = ACTION_TAGS_KEY,
                  ) -> List[Tuple[str, str]]:
    """The (tag, source) list reaching below the dict `d` at `path`: the
    `chain` arriving from above minus what `d`'s
    exclude_inherited_action_tags cuts, plus `d`'s own `own_key` tags."""
    out = list(chain)
    if ACTION_TAG_EXCLUDE_KEY in d:
        raw = d[ACTION_TAG_EXCLUDE_KEY]
        if isinstance(raw, str) and raw.strip().lower() in ("all", "*"):
            out = []
        else:
            cut = set(_tag_field(d, ACTION_TAG_EXCLUDE_KEY, path, problems))
            out = [(t, s) for t, s in out if t not in cut]
    if own_key is not None:
        out += [(t, path) for t in _tag_field(d, own_key, path, problems)]
    return out


# ----------------------------------------------------------------------
# Container scope rules
# ----------------------------------------------------------------------

def _split_csv(value: Any) -> List[str]:
    """Parse the comma-separated string-list shape used by the action
    container/allowlist rules. Whitespace around items is stripped;
    empty items are dropped."""
    if not isinstance(value, str):
        return []
    return [tok.strip() for tok in value.split(",") if tok.strip()]


def _log_target_str(target: Any) -> str:
    """Compact string form of an action's resolved target for the event
    log: entity id as-is, a (x, y) location as '(x,y)', a list joined by
    ', ', and None (target=none) as ''."""
    if target is None:
        return ""
    if isinstance(target, str):
        return target
    if isinstance(target, (tuple, list)):
        if (len(target) == 2 and all(isinstance(c, int) for c in target)):
            return f"({target[0]},{target[1]})"
        return ", ".join(_log_target_str(t) for t in target)
    return str(target)


def _top_container_allowed(top_key: str, rules: Dict[str, Any]) -> bool:
    """True iff the top-level vars key `top_key` should be descended
    into by the action discovery walker. Consults
    action_container_mode + action_container_paths."""
    mode = rules.get("action_container_mode", "all")
    paths = _split_csv(rules.get("action_container_paths", ""))
    if mode == "all":
        return True
    if mode == "whitelist":
        return top_key in paths
    if mode == "blacklist":
        return top_key not in paths
    # Unknown mode: behave like "all" rather than silently hiding
    # everything. Rule schema validation should catch the bad value
    # before it gets here.
    return True


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------

def discover_actions(
    entity: "Entity", rules: Dict[str, Any],
) -> Dict[str, List[Action]]:
    """Walk `entity.vars` and return every action keyed by name.

    Result shape: `{action_name: [Action, ...]}` — the list is per-
    name because two containers can both declare an action of the
    same name (e.g. two swords both having `slice`), in which case
    the caller (the !action handler) shows a disambiguation menu.

    Discovery rules:
      - any vars subtree whose IMMEDIATE child key is `actions` and
        whose `actions` value is a dict, contributes its children as
        actions of the enclosing container
      - depth is unbounded — `inventory.bags.left.sword.actions.slice`
        is just as valid as `inventory.sword.actions.slice`
      - the `actions` subkey is itself skipped during descent (we
        don't recurse into an action's `body` looking for nested
        action dicts; bodies are strings anyway)
      - the action_container_mode/paths rules gate WHICH top-level
        containers to descend into. Mode `all` searches everything;
        whitelist/blacklist scope to the named first-segments. A
        top-level `actions` container (entity.vars.actions.<name>) is
        always considered regardless of the rules (it's the
        "intrinsic actions" slot, not a container being scoped).
      - malformed action subtrees (missing `body`, wrong shape) are
        SKIPPED with no error — discovery is read-only and tolerant.
        The validation-error path is for the explicit invocation
        attempt. (We don't want one broken action to make the entity's
        whole action list unreadable.)

    The result preserves the order Python dict iteration produces
    (insertion order), so `entity_actions()` and the disambiguation
    menu give a stable view across calls."""
    out: Dict[str, List[Action]] = {}
    # The vars root is a container too: its action_tags reach every action.
    problems: List[str] = []
    root_chain = _inherit_tags([], entity.vars, "", problems)

    # Top-level intrinsic actions: entity.vars.actions.<name>. Bypass
    # the container scoping (it's the catch-all slot for entity-level
    # actions that don't belong to any item/skill/etc.).
    intrinsic = entity.vars.get(ACTIONS_KEY)
    if isinstance(intrinsic, dict):
        _add_actions(intrinsic, "", root_chain, problems, out)

    # Container actions. Each top-level key (subject to the container
    # rules) is the root of a recursive walk that looks for `.actions.`
    # subdicts at any depth. Note that the walker descends INTO each
    # container but skips the `actions` key when it finds one (no
    # actions-within-actions).
    # The disguise var holds a decoy statblock (its `vars.actions` are what
    # a fooled viewer is shown), never the unit's own actions.
    disguise = str(rules.get("disguise_var", "disguise"))
    for top_key, top_val in entity.vars.items():
        if top_key == ACTIONS_KEY or top_key == disguise:
            continue  # already handled above / a decoy
        if not _top_container_allowed(top_key, rules):
            continue
        if not isinstance(top_val, dict):
            continue
        _walk_for_actions(
            top_val, base_path=top_key, out=out,
            chain=root_chain, problems=problems,
        )
    holder = getattr(entity, "id", None)
    for acts in out.values():
        for act in acts:
            act.holder = holder
    return out


def _add_actions(actions_subtree: Dict[str, Any], base_path: str,
                 chain: List[Tuple[str, str]], problems: List[str],
                 out: Dict[str, List[Action]]) -> None:
    """Each action of the `actions` dict of the container at `base_path`
    ("" = the vars root) into `out`, with the tags arriving from above
    (`chain`) and the `actions` dict's own tag fields."""
    path = f"{base_path}.{ACTIONS_KEY}" if base_path else ACTIONS_KEY
    probs = list(problems)
    chain = _inherit_tags(chain, actions_subtree, path, probs)
    for name, raw in actions_subtree.items():
        if name in _ACTIONS_DICT_FIELDS:
            continue
        try:
            act = Action.from_dict(
                name, raw, container_path=base_path,
                full_path=f"{path}.{name}",
                inherited=chain, inherited_problems=probs,
            )
        except ActionValidationError:
            continue
        out.setdefault(name, []).append(act)


def _walk_for_actions(
    container: Dict[str, Any], *,
    base_path: str, out: Dict[str, List[Action]],
    chain: List[Tuple[str, str]], problems: List[str],
) -> None:
    """Recursive helper for discover_actions. Walks `container` and:
      - if it has an `actions` key with a dict value, every child of
        that dict is an Action of `base_path`
      - for every OTHER child that's itself a dict, recurses with the
        extended path (so nested containers like
        `inventory.bags.left.sword` are reachable)
    Non-dict children are leaves and ignored. The `actions` subkey
    is NEVER recursed into. `chain` is the (tag, source) list of the
    action_tags reaching `container` from above, `problems` the tag
    fields above it that weren't tag lists."""
    problems = list(problems)
    chain = _inherit_tags(chain, container, base_path, problems)
    actions_subtree = container.get(ACTIONS_KEY)
    if isinstance(actions_subtree, dict):
        _add_actions(actions_subtree, base_path, chain, problems, out)
    for k, v in container.items():
        if k == ACTIONS_KEY:
            continue
        if isinstance(v, dict):
            _walk_for_actions(
                v,
                base_path=f"{base_path}.{k}",
                out=out, chain=chain, problems=problems,
            )


def discover_mount_actions(
    rider: "Entity", match: "Match", rules: Dict[str, Any],
) -> Dict[str, List[Action]]:
    """Actions a MOUNTED rider can invoke through its vehicle, same
    `{name: [Action, ...]}` shape as discover_actions. Two sources:
      - SLOT actions: defined at `vehicle.vars.slots.<slot>.actions.<name>`
        — available only to riders in that slot (container_path ==
        `slots.<slot>`).
      - VEHICLE actions with `allowed_slots`: any action on the vehicle
        (outside the slots subtree) whose `allowed_slots` list includes the
        rider's slot (or "*" / "all").
    A vehicle action with NO allowed_slots, and any OTHER slot's actions,
    are excluded. Empty when the rider isn't mounted / the host is gone."""
    if not rider.is_mounted:
        return {}
    veh = match.entities.get(rider.mounted_on)
    if veh is None:
        return {}
    slot = rider.mount_slot
    slot_container = f"slots.{slot}"
    out: Dict[str, List[Action]] = {}
    for name, acts in discover_actions(veh, rules).items():
        for act in acts:
            cp = act.container_path
            if cp == slot_container:
                avail = True                       # this slot's own action
            elif cp.startswith("slots."):
                avail = False                      # another slot's action
            elif act.allowed_slots is not None:
                al = act.allowed_slots
                avail = (slot in al) or ("*" in al) or ("all" in al)
            else:
                avail = False                      # vehicle-private action
            if avail:
                out.setdefault(name, []).append(act)
    return out


def fold_name(name: str, rules: Dict[str, Any]) -> str:
    """Apply the action_names_case_sensitive rule to a name token. If
    case-sensitive (default), returned unchanged. If False, lowercased.
    Used by the !action handler when matching the user's typed name
    against the discovered name set."""
    if rules.get("action_names_case_sensitive", True):
        return name
    return name.lower()


def lookup_action(
    actions_by_name: Dict[str, List[Action]],
    requested: str, rules: Dict[str, Any],
) -> List[Action]:
    """Resolve a user-typed action name against the discovery result.
    Returns the list of matching actions (length 0, 1, or more — the
    caller decides what to do with each). Respects
    action_names_case_sensitive."""
    case_sensitive = rules.get("action_names_case_sensitive", True)
    if case_sensitive:
        return list(actions_by_name.get(requested, []))
    target = requested.lower()
    matches: List[Action] = []
    for name, acts in actions_by_name.items():
        if name.lower() == target:
            matches.extend(acts)
    return matches


# ----------------------------------------------------------------------
# Source / args proxies
# ----------------------------------------------------------------------

class SourceProxy:
    """Body-language binding that mirrors `entity[self]` reads/writes
    but rooted at a fixed sub-path of the entity's vars.

    For an action at `inventory.sword.actions.slice`, `source` is a
    SourceProxy whose base_path is `inventory.sword`. Then:

        source.damage             reads entity.vars.inventory.sword.damage
        source.damage = source.damage - 1
                                  writes through Entity.write_var so
                                  on_var_changed fires the same way it
                                  would for `entity[self].inventory
                                  .sword.damage = ...`

    For a top-level action (container_path == "") the proxy's base
    path is the empty string, so `source.<path>` is functionally
    identical to `entity[self].<path>`.

    The proxy reserves underscore-prefixed attribute names for its own
    internal slots (`_entity`, `_base_path`) — those aren't legal vars
    key shapes anyway (vars keys are user-defined strings, conventionally
    not starting with `_`)."""

    __slots__ = ("_entity", "_base_path")

    def __init__(self, entity: "Entity", base_path: str):
        object.__setattr__(self, "_entity", entity)
        object.__setattr__(self, "_base_path", base_path)

    def _full(self, attr: str) -> str:
        if self._base_path:
            return f"{self._base_path}.{attr}"
        return attr

    def __getattr__(self, attr: str) -> Any:
        # Underscore-prefixed lookups bypass — they're the slots
        # we set in __init__ and don't represent vars keys.
        if attr.startswith("_"):
            raise AttributeError(attr)
        path = self._full(attr)
        cur: Any = self._entity.vars
        for seg in path.split("."):
            if not isinstance(cur, dict) or seg not in cur:
                # Re-raise as AttributeError so the formula engine's
                # error-message machinery surfaces it as a "no var at
                # path" rather than a Python attribute miss.
                raise AttributeError(
                    f"`{self._entity.id}` has no var at "
                    f"'{path}' (via source)."
                )
            cur = cur[seg]
        return cur

    def __setattr__(self, attr: str, value: Any) -> None:
        if attr.startswith("_"):
            object.__setattr__(self, attr, value)
            return
        path = self._full(attr)
        log = self._entity.write_var(path, value)
        # Hook output of the write joins the action's output (like an
        # `entity[x].path = ...` write; it used to be dropped).
        m = getattr(self._entity, "_match", None)
        if m is not None:
            m.surface_log(log)


def _check_on_map(match: Any, x: int, y: int,
                  actor: Optional[str] = None,
                  map_name: Optional[str] = None) -> "Coord":
    """A location target names a cell of the map (one off it used to run
    the body on a cell nothing can occupy): the map it names, else the
    acting unit's map, else the match's only map. Returns the Coord, which
    carries that map."""
    ae = match.entities.get(actor) if actor else None
    home = match.map_of(ae) if ae is not None else match.get_map()
    mp = home if map_name is None else match.get_map(map_name)
    if not mp.in_bounds(x, y):
        if mp.in_rect(x, y):
            raise VTTError(f"location ({x},{y}) is a void cell.")
        raise VTTError(f"location ({x},{y}) is off the map "
                       f"({mp.size_text()}).")
    return Coord(x, y, mp.name, named=mp is not home)


class Coord:
    """Tiny `.x` / `.y` value object used for `target` bindings on
    location-typed actions. The formula sandbox rejects subscript
    (`target[0]`) so a tuple wouldn't be usable from the body; an
    object with attributes is the consistent shape with everything
    else the body sees (entity[X].x, source.x, etc.).

    Mutable by intent — assigning to `.x` / `.y` updates the local
    copy but does NOT write through to anything in the match (a
    coordinate is just a value). For *_list targets, the runner
    wraps each tuple individually so a `for loc in target:` loop
    binds `loc` to one Coord per iteration."""

    __slots__ = ("x", "y", "map", "named")

    def __init__(self, x: int, y: int, map: Optional[str] = None,
                 named: bool = False):
        self.x = x
        self.y = y
        # The map the cell is on (`target.map`); `named` when it isn't the
        # acting unit's, so a stored copy says which map.
        self.map = map
        self.named = named

    def __repr__(self) -> str:
        return f"Coord({self.x}, {self.y})"

    def store_value(self) -> List[Any]:
        """How a var stores it: a coordinate list [x, y], like every other
        stored coordinate (logic._own_value), or [x, y, map] for a cell on
        another map than the acting unit's."""
        if self.named and self.map is not None:
            return [self.x, self.y, self.map]
        return [self.x, self.y]

    def __iter__(self):
        # Allow `for cx, cy in [coord1, coord2]:` style unpacking
        # (the action validator's `for (cx, cy) in ...` form expects
        # 2-tuple-shaped iterables).
        yield self.x
        yield self.y


class ArgsProxy:
    """Body-language binding over the GM-supplied args dict. Attribute
    access only — `args.amount` reads, `args.amount = ...` writes
    (writes are local to the proxy; they DON'T persist beyond this
    action call). Missing attributes raise AttributeError (which the
    formula engine surfaces as "Unknown identifier" or similar)."""

    __slots__ = ("_data",)

    def __init__(self, data: Dict[str, Any]):
        object.__setattr__(self, "_data", dict(data))

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        d = object.__getattribute__(self, "_data")
        if attr not in d:
            raise AttributeError(
                f"action args has no `{attr}` (passed args: "
                f"{sorted(d.keys()) or 'none'})."
            )
        return d[attr]

    def __setattr__(self, attr: str, value: Any) -> None:
        if attr.startswith("_"):
            object.__setattr__(self, attr, value)
            return
        d = object.__getattribute__(self, "_data")
        d[attr] = value

    def __contains__(self, key: str) -> bool:
        d = object.__getattribute__(self, "_data")
        return key in d


# ----------------------------------------------------------------------
# Argument coercion (for `key=value` tokens on the command line)
# ----------------------------------------------------------------------

def coerce_args_token(value: str) -> Any:
    """Best-effort scalar coercion of a value typed on the command
    line. Matches the convention used by !find and similar commands:
    `true`/`false` → bool, integer → int, float → float, otherwise
    raw string. Quotation marks aren't preserved — shlex already
    stripped them at the command-parsing layer."""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse_args_tokens(tokens: List[str]) -> Dict[str, Any]:
    """Parse `key=value` tokens into the args dict. Tokens without `=`
    are rejected so a typo doesn't silently become an empty-named
    key. Duplicate keys: last write wins (matches shell convention).
    """
    out: Dict[str, Any] = {}
    for tok in tokens:
        if "=" not in tok:
            raise VTTError(
                f"action arg `{tok}` must be in `key=value` form."
            )
        k, _, v = tok.partition("=")
        if not k:
            raise VTTError(
                f"action arg `{tok}` has an empty key."
            )
        out[k] = coerce_args_token(v)
    return out


# ----------------------------------------------------------------------
# Target parsing (per declared target_type)
# ----------------------------------------------------------------------

def parse_target(
    target_type: str, tokens: List[str], match: "Match",
    actor: Optional[str] = None,
    missing: Optional[Any] = None,
    missing_corpse: Optional[Any] = None,
    map_of: Optional[Any] = None,
) -> Tuple[Any, List[str]]:
    """Consume the leading tokens that constitute the target and
    return `(target_value, remaining_tokens)`. The remaining tokens
    are the args portion (key=value). Raises VTTError on shape
    violations.

    target_type semantics:
      none           consumes 0 tokens; target value is None
      entity         consumes 1 token (an entity id, or `self`/`this`/
                     `current`); value is the resolved eid string
      location       consumes 2 tokens (x then y, ints); value is (x,y)
      entity_list    consumes EVERY non-key=value token (entity ids);
                     value is List[str]
      location_list  consumes paired x/y tokens (must be even count);
                     value is List[Tuple[int,int]]

    For the *_list variants the boundary between target tokens and
    args is "first token containing `=`". So `!action self lightning
    3 4 5 6 power=10` parses as target=[(3,4),(5,6)] and args={power:10}.

    A location / location_list target takes `map=<map>` among the tokens
    (anywhere): the map its cells are on (default the actor's), as every
    other command with coordinates names one; `map_of(spec)` turns the word
    into a map name (the caller's view rules), and the key is not an
    action arg for those targets.
    """
    # `missing(eid, token)`: the caller's test for a unit to treat as
    # missing (one the channel's view hides), raising its own refusal.
    def check(eid: str, token: str) -> None:
        if missing is not None:
            missing(eid, token)
        elif eid not in match.entities:
            raise NotFound(f"Entity '{eid}' not found.")

    # `missing_corpse(cid)`: the same for a corpse (one the view hides).
    def check_corpse(cid: str) -> None:
        if missing_corpse is not None:
            missing_corpse(cid)
        elif match.find_corpse(cid) is None:
            raise NotFound(f"No corpse with id '{cid}'.")
    map_name: Optional[str] = None
    if target_type in ("location", "location_list"):
        rest: List[str] = []
        for t in tokens:
            if t.startswith("map="):
                if map_name is not None:
                    raise VTTError("map= is given twice.")
                map_name = t[4:]
                if not map_name:
                    raise VTTError("map= needs a map name.")
            else:
                rest.append(t)
        tokens = rest
        if map_name is not None and map_of is not None:
            map_name = map_of(map_name)
    if target_type == "none":
        return None, list(tokens)
    if target_type == "entity":
        if not tokens:
            raise VTTError("expected an entity id after the action name.")
        eid_token = tokens[0]
        eid = _resolve_eid_for_action(eid_token, match)
        check(eid, eid_token)
        return eid, list(tokens[1:])
    if target_type == "location":
        if len(tokens) < 2:
            raise VTTError(
                "expected `<x> <y>` coords after the action name."
            )
        try:
            x = int(tokens[0]); y = int(tokens[1])
        except ValueError:
            raise VTTError(
                f"location target needs integer coords; got "
                f"`{tokens[0]} {tokens[1]}`."
            )
        return (_check_on_map(match, x, y, actor, map_name),
                list(tokens[2:]))
    if target_type == "entity_list":
        eids: List[str] = []
        i = 0
        while i < len(tokens) and "=" not in tokens[i]:
            eid = _resolve_eid_for_action(tokens[i], match)
            check(eid, tokens[i])
            eids.append(eid)
            i += 1
        if not eids:
            raise VTTError("entity_list target needs at least one entity.")
        return eids, list(tokens[i:])
    if target_type == "location_list":
        coords: List[Coord] = []
        i = 0
        while i + 1 < len(tokens) and "=" not in tokens[i] and "=" not in tokens[i + 1]:
            try:
                x = int(tokens[i]); y = int(tokens[i + 1])
            except ValueError:
                break
            coords.append(_check_on_map(match, x, y, actor, map_name))
            i += 2
        if not coords:
            raise VTTError(
                "location_list target needs at least one paired `<x> <y>`."
            )
        return coords, list(tokens[i:])
    if target_type == "corpse":
        if not tokens:
            raise VTTError("expected a corpse id after the action name.")
        cid = tokens[0]
        check_corpse(cid)
        return cid, list(tokens[1:])
    if target_type == "corpse_list":
        cids: List[str] = []
        i = 0
        while i < len(tokens) and "=" not in tokens[i]:
            check_corpse(tokens[i])
            cids.append(tokens[i])
            i += 1
        if not cids:
            raise VTTError("corpse_list target needs at least one corpse id.")
        return cids, list(tokens[i:])
    # Should be unreachable given ALLOWED_TARGET_TYPES.
    raise VTTError(f"unknown target type `{target_type}`.")


def _resolve_eid_for_action(token: str, match: "Match") -> str:
    """Tiny shim that resolves `self`/`this`/`current` to the current
    entity id, leaving other tokens unchanged. Mirrors the convention
    used by other commands that accept those shortcuts. Defined here
    (rather than importing from vtt_commands) to keep the action
    module loadable without the command layer."""
    t = token.strip().lower()
    if t in {"self", "this", "current"}:
        eid = match.current_entity_id()
        if not eid:
            raise NotFound("No current entity (turn order is empty).")
        return eid
    return token


def dedupe_targets(target_type: str, value: Any,
                   name: str) -> Tuple[Any, Optional[str]]:
    """A list target with each unit / cell / corpse once (first-seen
    order), and a warning naming the repeats, or None. Listing one twice
    used to run the body on it twice (user call: merge and say so; to act
    on something twice, run the action twice)."""
    if not target_type.endswith("_list") or not isinstance(value, list):
        return value, None
    seen: Set[Any] = set()
    out: List[Any] = []
    dups: List[str] = []
    for v in value:
        k = (v.x, v.y) if isinstance(v, Coord) else v
        if k in seen:
            label = f"({k[0]},{k[1]})" if isinstance(k, tuple) else f"`{k}`"
            if label not in dups:
                dups.append(label)
            continue
        seen.add(k)
        out.append(v)
    if not dups:
        return value, None
    return out, (f"⚠️ {', '.join(dups)} listed more than once in `{name}`'s "
                 f"targets: the action runs on each once. To act on one "
                 f"twice, run the action twice.")


def coerce_target(target_type: str, value: Any, match: "Match",
                  name: str, actor: Optional[str] = None) -> Any:
    """use_action()'s target turned into what the action's body gets from
    `!action` (parse_target): an entity id, a Coord (from [x, y] or a
    Coord), a list of either, a corpse id or None, checked the same way.
    A coordinate list used to reach the body as a list, so `target.x`
    failed with a Python error."""
    def _coord(v: Any) -> "Coord":
        mname = None
        if isinstance(v, Coord):
            x, y, mname = v.x, v.y, v.map
        elif (isinstance(v, (list, tuple)) and len(v) in (2, 3)
              and all(isinstance(c, int) and not isinstance(c, bool)
                      for c in v[:2])
              and (len(v) == 2 or isinstance(v[2], str))):
            x, y = v[0], v[1]
            mname = v[2] if len(v) == 3 else None
        else:
            raise VTTError(f"action `{name}` takes a location target: "
                           f"[x, y] or [x, y, map], got {v!r}.")
        return _check_on_map(match, x, y, actor, mname)

    def _eid(v: Any) -> str:
        if not isinstance(v, str) or not v:
            raise VTTError(f"action `{name}` takes an entity target: an id, "
                           f"got {v!r}.")
        eid = _resolve_eid_for_action(v, match)
        if eid not in match.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        return eid

    def _cid(v: Any) -> str:
        if not isinstance(v, str) or match.find_corpse(v) is None:
            raise NotFound(f"No corpse with id {v!r}.")
        return v

    def _many(each: Any, what: str) -> List[Any]:
        if not isinstance(value, (list, tuple)) or not value:
            raise VTTError(f"action `{name}` takes a non-empty list of "
                           f"{what}, got {value!r}.")
        return [each(v) for v in value]

    if target_type == "none":
        if value is not None:
            raise VTTError(f"action `{name}` takes no target, got {value!r}.")
        return None
    if target_type == "entity":
        return _eid(value)
    if target_type == "location":
        return _coord(value)
    if target_type == "entity_list":
        return _many(_eid, "entity ids")
    if target_type == "location_list":
        return _many(_coord, "[x, y] locations")
    if target_type == "corpse":
        return _cid(value)
    if target_type == "corpse_list":
        return _many(_cid, "corpse ids")
    return value


# ----------------------------------------------------------------------
# Allowlist parsing (used by the cmd() builtin)
# ----------------------------------------------------------------------

def parse_cmd_allowlist(rules: Dict[str, Any]) -> "frozenset[str]":
    """Return the set of command names an action body's cmd() can
    dispatch. Pulled from the action_cmd_allowlist rule. Empty when
    the rule is empty (cmd() then refuses every dispatch)."""
    return frozenset(_split_csv(rules.get("action_cmd_allowlist", "")))


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------

async def run_action(
    action: Action, *,
    actor_id: str,
    target: Any,
    args: Dict[str, Any],
    match: "Match",
    mgr: Any,           # MatchManager (kept loose to avoid import cycle)
    ctx: Any,           # ReplyContext for cmd()'s dispatch output
    answers: Optional[List[Any]] = None,  # pre-supplied choose() answers
    extra_ctx: Optional[Dict[str, Any]] = None,  # extra hook-ctx bindings
    # Reactions (#36). `steps`: the programs to run, in order, as one
    # transaction (default prepare, cost, body: those the action has).
    # `preset_locals`: locals the first step starts with (a resolving
    # body gets its prepare's). `prepare_out` / `body_out`: filled with
    # those steps' locals. `finish`: run the success tail (say_format,
    # hooks, the log); a prepare-only run doesn't. `skip_checks`: the
    # restrictions and target_condition were judged already (a
    # resolution re-checks them itself). `extra_bindings`: merged into the
    # body's bindings last. `fail_out`: gets "refund" when fail(refund).
    # `target_out`: gets the target as the checks left it ("target") and
    # how many choice answers the run used ("answers_used").
    steps: Optional[Tuple[str, ...]] = None,
    preset_locals: Optional[Dict[str, Any]] = None,
    prepare_out: Optional[Dict[str, Any]] = None,
    body_out: Optional[Dict[str, Any]] = None,
    finish: bool = True,
    skip_checks: bool = False,
    extra_bindings: Optional[Dict[str, Any]] = None,
    fail_out: Optional[Dict[str, Any]] = None,
    target_out: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    """Execute an action body transactionally.

    Returns (success, fail_message). On success, fail_message is None.
    On a clean fail(...) abort, success is False and fail_message is
    the GM-supplied reason (the runner has already rolled back the
    pre-state and the !action handler should surface
    `❌ <action_name>: <message>`).

    On an unexpected runtime error (FormulaError or other Exception),
    the runner ALSO rolls back, but re-raises so the dispatcher's
    standard `💥 Unexpected error: ...` path catches it.

    Recursion: an action body's `cmd()` or `use_action()` calls
    increment match._action_depth; the runner enforces
    action_recursion_limit at the top of every nested call.
    """
    from logic import Match  # for type hint, no runtime impact
    from formula import FormulaEngine, EvalCtx

    # Enforce recursion limit BEFORE allocating any state for this
    # call. The check uses the CURRENT depth — _action_depth gets
    # bumped just below, so the limit value is "max nested actions
    # including this one". This is an ENGINE FAULT (raised) rather
    # than a clean fail (returned), so use_action() can't accidentally
    # swallow it by ignoring the False return — the exception
    # propagates up the whole chain so EVERY level rolls back.
    limit = int(match.rules.get("action_recursion_limit", 8))
    if match._action_depth >= limit:
        raise ActionEngineFault(
            f"action recursion limit reached ({limit}). The action "
            f"`{action.name}` would be call #{match._action_depth + 1} "
            f"in a chain — break the cycle or raise "
            f"`action_recursion_limit`."
        )

    actor = match.entities.get(actor_id)
    if actor is None:
        raise NotFound(f"Action actor '{actor_id}' not found.")

    # Snapshot pre-state for transactional rollback on failure. The
    # match-history layer handles serialization; we just stash the
    # pre-state dict (no history-snapshot append yet — that happens
    # in the dispatcher AFTER the action completes).
    pre_state = match.to_dict(include_history=False)
    hist_mark = match.history.mark()
    pre_action_depth = match._action_depth
    is_top_level = (pre_action_depth == 0)

    # Buffer for command output produced by cmd() inside the body.
    # cmd() can't dispatch into the real (awaitable) ctx because the
    # formula engine is synchronous — so it dispatches into a
    # _BufferCtx whose send() never truly suspends, and we flush the
    # buffer through the real ctx after a SUCCESSFUL top-level action.
    # Nested sub-actions reuse the top-level buffer (stored on the
    # match) so all command echoes flush together, once, at the top.
    if is_top_level:
        buffer_ctx = _BufferCtx.for_ctx(ctx)
        match._runtime_buffer = buffer_ctx
    else:
        buffer_ctx = getattr(match, "_runtime_buffer", None)
        if buffer_ctx is None:
            buffer_ctx = _BufferCtx.for_ctx(ctx)
            match._runtime_buffer = buffer_ctx

    # Build the action's eval bindings. SourceProxy/ArgsProxy
    # implement the body-side magic; cmd/fail are closure-built so
    # they can see this runner's match + buffer + allowlist.
    source = SourceProxy(actor, action.container_path)
    args_proxy = ArgsProxy(args)
    cmd_allowlist = parse_cmd_allowlist(match.rules)
    # The other matches as they were before this level's first cmd() line
    # that can change them (see _cmd); None until one runs.
    cross: Dict[str, Any] = {"snap": None}

    def _rollback_all() -> None:
        _rollback_match(match, mgr, pre_state)
        # Autosaves the rolled-back body recorded (a turn advanced by a
        # cmd() line) go too, and the ones pruned meanwhile return.
        match.history.rewind(hist_mark)
        if cross["snap"] is not None:
            _restore_other_matches(mgr, match, cross["snap"])
            cross["snap"] = None

    def _guard_cross() -> None:
        """Save the other matches before this level's first change to one
        of them (a transfer_entity in the body), so _rollback_all puts
        them back too. Every enclosing action level that has no copy yet
        takes the same one: a nested action that moved a unit and
        succeeded used to leave it in both matches when its caller then
        failed."""
        levels = [lvl for lvl in _CROSS_LEVELS.get() + (cross,)
                  if lvl["snap"] is None]
        if levels:
            snap = _snapshot_other_matches(mgr, match)
            for lvl in levels:
                lvl["snap"] = snap

    def _cmd(line: Any) -> None:
        """cmd('<command line>'): dispatch the line as if the GM
        typed it. shlex-tokenized; first token must be an
        action_cmd_allowlist member. Raises ActionFail on a
        disallowed or malformed line — the body can preempt-check
        instead by reading from vars / using has_action / etc."""
        if not isinstance(line, str) or not line.strip():
            raise ActionFail(
                "cmd(...) needs a non-empty string command line."
            )
        # Lazy import: action.py is loaded by vtt_commands at module
        # init, so importing the registry at module scope would
        # cycle. Inside this closure we're past both modules' loads.
        from vtt_commands import (registry, ASSERT_STOP,  # noqa: PLC0415
                                  split_command_line)
        try:
            tokens = split_command_line(line)
        except ValueError as ex:
            raise ActionFail(f"cmd(): cannot parse `{line}`: {ex}")
        if not tokens:
            raise ActionFail("cmd(): line is empty after parsing.")
        name = tokens[0]
        if name not in cmd_allowlist:
            allowed = ", ".join(sorted(cmd_allowlist)) or "(none)"
            raise ActionFail(
                f"cmd(): command `{name}` is not on this system's "
                f"action_cmd_allowlist. Allowed: {allowed}."
            )
        # A line that can change ANOTHER match (`ent transfer` / `ent copy`
        # into it, `match ...`): the other matches are saved before the
        # first such line of this action level, so a rollback puts them
        # back with this match (a failed send-to-another-battlefield action
        # left the unit in both matches).
        sub = tokens[1].lower() if len(tokens) > 1 else ""
        if name == "match" or (name == "ent" and sub in ("transfer", "copy")):
            _guard_cross()
        # Dispatch into the synchronous buffer context. Because
        # _BufferCtx.send never suspends on a real future, the
        # dispatched (async) command coroutine runs to completion
        # under a single _sync_dispatch pump — no event loop needed,
        # so this works identically whether we're under the CLI's
        # asyncio.run, the scenario harness, or the live Discord
        # bot's running loop. The dispatcher's per-command snapshot is
        # suppressed because match._action_depth > 0 (set below).
        # Use the CURRENT buffer (the replay loop swaps in a fresh one
        # per attempt, so reference it dynamically rather than capturing).
        from vtt_commands import _GATE_LINES  # noqa: PLC0415
        coro = registry.dispatch_no_snapshot(
            tokens[0], tokens[1:], match._runtime_buffer or buffer_ctx, mgr,
        )
        # The body is GM-written: its lines aren't a caller's typed lines,
        # even when the action ran from a batch (_GATE_LINES).
        gate_token = _GATE_LINES.set(False)
        try:
            result = _sync_dispatch_returning(coro)
        finally:
            _GATE_LINES.reset(gate_token)
        # A failed `!assert` line stops the body like fail() does (rolled
        # back); the ❌ Assertion failed line is already in the buffer.
        if result == ASSERT_STOP:
            raise ActionFail(f"cmd(): `{line}` failed its assertion.",
                             reason="assert")

    def _fail(*fail_args: Any, refund: Any = False) -> None:
        """fail(message) | fail(reason, message): abort the action
        body cleanly. The runner catches ActionFail, rolls back to
        pre-state, fires on_action_failed (when reachable), and
        surfaces `❌ <action>: [<reason>] <message>` (the [reason]
        prefix is dropped when reason is empty). Always raises —
        control does NOT return to the body.

        Reason is a GM-chosen tag like "cost", "out_of_range",
        "invalid_target". It's surfaced to on_action_failed passives
        via the `fail_reason` binding so reactive logic can
        discriminate between failure modes. The word `refund` among the
        arguments (or refund=True) makes a body failing after reactions
        ran roll the whole exchange back, the reactions too (#36)."""
        refund_flag = bool(refund) or any(type(a) is _RefundWord
                                          for a in fail_args)
        fail_args = tuple(a for a in fail_args if type(a) is not _RefundWord)
        if not fail_args:
            raise ActionFail("the action failed.", reason="",
                             refund=refund_flag)
        if len(fail_args) == 1:
            reason = ""
            message = str(fail_args[0])
        elif len(fail_args) == 2:
            reason = str(fail_args[0])
            message = str(fail_args[1])
        else:
            raise ActionFail(
                "fail() takes 1 or 2 arguments: fail(message) or "
                "fail(reason, message), plus the word refund.",
                reason="usage",
            )
        raise ActionFail(message, reason=reason, refund=refund_flag)

    def _choose(prompt: Any, options: Any) -> Any:
        """choose(prompt, options): present `options` (a list) and return
        the chosen element. Consumes the next pre-supplied answer if any,
        else raises ChoiceNeeded so the top-level runner prompts and
        replays. A pre-supplied answer that matches no option (or the
        reserved 'cancel' token) aborts the action."""
        if not isinstance(options, (list, tuple)):
            raise ActionFail(
                "choose(prompt, options): options must be a list.",
                reason="usage")
        opts = list(options)
        if not opts:
            raise ActionFail(
                f"choose('{prompt}', ...): the options list is empty.",
                reason="usage")
        cur = match._choice_cursor
        ans = match._choice_answers
        if cur < len(ans):
            raw = ans[cur]
            match._choice_cursor = cur + 1
            if isinstance(raw, str) and raw.strip().lower() in CHOICE_CANCEL_TOKENS:
                raise ActionFail("choice cancelled.", reason="cancelled")
            picked = _match_option(raw, opts)
            if picked is _CHOICE_MISS:
                raise ActionFail(
                    f"invalid choice {raw!r} for '{prompt}' "
                    f"(options: {opts}).", reason="invalid_choice")
            return picked
        raise ChoiceNeeded(str(prompt), options=opts)

    def _choose_number(prompt: Any, lo: Any, hi: Any) -> int:
        """choose_number(prompt, lo, hi): return a chosen integer in
        [lo, hi]. Same answer/replay model as choose()."""
        try:
            lo_i = int(lo); hi_i = int(hi)
        except (TypeError, ValueError):
            raise ActionFail(
                "choose_number(prompt, lo, hi): lo and hi must be integers.",
                reason="usage")
        if lo_i > hi_i:
            raise ActionFail(
                f"choose_number('{prompt}', {lo_i}, {hi_i}): lo > hi.",
                reason="usage")
        cur = match._choice_cursor
        ans = match._choice_answers
        if cur < len(ans):
            raw = ans[cur]
            match._choice_cursor = cur + 1
            if isinstance(raw, str) and raw.strip().lower() in CHOICE_CANCEL_TOKENS:
                raise ActionFail("choice cancelled.", reason="cancelled")
            try:
                n = int(raw)
            except (TypeError, ValueError):
                raise ActionFail(
                    f"invalid number {raw!r} for '{prompt}'.",
                    reason="invalid_choice")
            if not (lo_i <= n <= hi_i):
                raise ActionFail(
                    f"{n} out of range [{lo_i},{hi_i}] for '{prompt}'.",
                    reason="invalid_choice")
            return n
        raise ChoiceNeeded(str(prompt), options=None, lo=lo_i, hi=hi_i)

    def _no_window(*_a: Any, **_k: Any) -> Any:
        raise ActionFail("incoming_of() / redirect() work only while an "
                         "action resolves its reactions.", reason="usage")

    bindings = {
        "source": source,
        "args": args_proxy,
        "target": target,
        "cmd": _cmd,
        "fail": _fail,
        "choose": _choose,
        "choose_number": _choose_number,
        "refund": REFUND,
        "reaction_target": None,
        "reaction_args": ArgsProxy({}),
        "incoming_of": _no_window,
        "redirect": _no_window,
    }
    if extra_bindings:
        bindings.update(extra_bindings)
    if steps is None:
        steps = ("prepare", "cost", "body")
    programs = [(k, getattr(action, k)) for k in steps
                if isinstance(getattr(action, k, ""), str)
                and getattr(action, k, "").strip()]
    if not programs and "body" in steps:
        # A reaction without a body of its own runs only through !react.
        return _finish_body_less(action, match, pre_action_depth,
                                 is_top_level)

    def _finish_action_fail(af: "ActionFail") -> Tuple[bool, Optional[str]]:
        """Shared tail for a clean abort (body fail() OR a cancelled /
        unanswerable choice). The caller has ALREADY rolled back to
        pre-state; this resets depth, drops the buffer, fires
        on_action_failed (passives see pre-action state), logs, and
        returns the (False, (reason, message)) tuple."""
        match._action_depth = pre_action_depth
        if is_top_level:
            match._runtime_buffer = None
        match.fire_action_failed(
            action_tags=action.tags,
            actor_id=actor_id,
            action_name=action.name,
            action_path=action.full_path,
            target=target,
            args=dict(args),
            fail_reason=af.reason,
            fail_message=af.message,
        )
        match.log_event(
            "action_failed", actor=actor_id,
            actor_name=match._entity_name(actor_id),
            action=action.name, action_path=action.full_path,
            target=_log_target_str(target),
            reason=af.reason, message=af.message,
        )
        return False, (af.reason, af.message)

    # Statuses that forbid the action, or make its targets immune (#234),
    # before the body runs; an immune unit in a list target is dropped.
    refusal = None if skip_checks else _restriction_refusal(
        action, actor_id, target, match, extra_ctx)
    if refusal is None and not skip_checks and action.target_condition:
        refusal = _target_condition_refusal(action, actor_id, target, match)
    if refusal is not None:
        reason, message, kept, notes = refusal
        if reason:
            return _finish_action_fail(ActionFail(message, reason=reason))
        if notes:
            if is_top_level:
                for note in notes:
                    await ctx.send(note)
            else:
                match.surface_log(notes)
        target = kept
        if not (extra_bindings and "target" in extra_bindings):
            bindings["target"] = target

    # The teams that see the actor as it begins: they see the action used
    # (recorded on success, below).
    seers = match.teams_seeing(actor_id)
    body_locals: Dict[str, Any] = {}
    match._action_depth += 1
    try:
        engine = FormulaEngine(match)
        extras = dict(extra_ctx) if extra_ctx else {}
        extras.setdefault("action_name", action.name)
        extras.setdefault("action_tags", list(action.tags))
        eval_ctx = EvalCtx(
            this=match.current_entity_id(),
            target=actor_id,
            extras=extras,
        )
        # Mid-body choices use a REPLAY model: only the top-level
        # invocation owns the loop + answer queue. It seeds the queue
        # from pre-supplied `answer=` tokens, then re-runs the body once
        # per interactive prompt (rolling back each attempt, replaying
        # the same RNG draws) until the body completes without needing a
        # new choice. A nested action just lets ChoiceNeeded propagate up
        # to this loop.
        if is_top_level:
            match._choice_answers = list(answers or [])
            rng_state = _snapshot_rng(match)
            # The per-command summon budget at action start. Each replay
            # attempt re-runs the body from the top (its summons are rolled
            # back between attempts), so the counter must reset to this — NOT
            # accumulate across attempts (which would falsely exhaust the
            # budget for a summon-before-choose action). _rollback_match now
            # PRESERVES _summon_count for the fail()/exception case, so we
            # restore the start value here explicitly for the replay case.
            summon_at_start = match._summon_count
            choice_limit = int(match.rules.get("action_choice_limit", 20))
        while True:
            if is_top_level:
                match._choice_cursor = 0
                match._summon_count = summon_at_start
                _restore_rng(match, rng_state)
                # Fresh output buffer per attempt — rolled-back attempts'
                # command echoes must not leak into the committed run.
                match._runtime_buffer = _BufferCtx.for_ctx(ctx)
            try:
                guard_token = CROSS_MATCH_GUARD.set(_guard_cross)
                levels_token = _CROSS_LEVELS.set(_CROSS_LEVELS.get()
                                                 + (cross,))
                # A body reads every var, private ones included (#70), its
                # cmd() lines too: `cmd('assert ...')` read through the
                # caller's reader, so the outcome hung on who ran it (L45).
                reader_token = VAR_READER.set(None)
                try:
                    carried: Dict[str, Any] = dict(preset_locals or {})
                    for kind, src in programs:
                        out_locals: Dict[str, Any] = {}
                        engine.eval_program(
                            src, eval_ctx,
                            action_mode=True,
                            action_bindings=bindings,
                            locals_out=out_locals,
                            locals_in=carried,
                        )
                        if kind == "prepare":
                            # The body starts with the prepare's locals.
                            carried = dict(carried, **out_locals)
                            if prepare_out is not None:
                                prepare_out.clear()
                                prepare_out.update(out_locals)
                        elif kind == "body":
                            body_locals.clear()
                            body_locals.update(out_locals)
                            if body_out is not None:
                                body_out.clear()
                                body_out.update(out_locals)
                finally:
                    VAR_READER.reset(reader_token)
                    _CROSS_LEVELS.reset(levels_token)
                    CROSS_MATCH_GUARD.reset(guard_token)
            except ChoiceNeeded as nc:
                if not is_top_level:
                    raise  # the top-level loop owns replay
                _rollback_all()
                if len(match._choice_answers) >= choice_limit:
                    match._action_depth = pre_action_depth
                    match._runtime_buffer = None
                    raise ActionEngineFault(
                        f"action_choice_limit reached ({choice_limit}) in "
                        f"`{action.name}` — the body asked for more choices "
                        f"than the limit (likely an unbounded choose loop)."
                    )
                try:
                    ans = await _obtain_answer(ctx, nc)
                    _log = INTERACTIVE_ANSWERS.get()
                    if _log is not None:
                        _log.append(ans)
                except ActionFail as af:
                    return _finish_action_fail(af)
                match._choice_answers.append(ans)
                continue
            except ActionFail as af:
                # Clean GM-initiated abort. Roll back, then finish.
                # Buffered command echoes are DISCARDED — the action
                # rolled back, so its partial output would mislead.
                _rollback_all()
                if fail_out is not None:
                    fail_out["refund"] = bool(getattr(af, "refund", False))
                return _finish_action_fail(af)
            except ActionEngineFault:
                # Engine refusal (recursion / choice limit). Roll back
                # AND re-raise so every level unwinds.
                _rollback_all()
                match._action_depth = pre_action_depth
                if is_top_level:
                    match._runtime_buffer = None
                raise
            except Exception:
                _rollback_all()
                match._action_depth = pre_action_depth
                if is_top_level:
                    match._runtime_buffer = None
                raise
            else:
                if target_out is not None:
                    target_out["answers_used"] = match._choice_cursor
                if is_top_level and finish:
                    # Answers the body never asked for (user call: warn,
                    # still run): a typo'd count, or a body that took
                    # another branch than the caller expected.
                    unused = match._choice_answers[match._choice_cursor:]
                    if unused:
                        match.surface_log([
                            f"⚠️ `{action.name}` asked for "
                            f"{match._choice_cursor} answer(s); unused: "
                            + ", ".join(f"`{a}`" for a in unused) + "."])
                break  # body completed without needing another choice
    finally:
        # Defensive: depth back to baseline no matter how we exit.
        if match._action_depth > pre_action_depth:
            match._action_depth = pre_action_depth
        # The choice queue is per-invocation; clear it once the
        # top-level call is done (success, fail, or error).
        if is_top_level:
            match._choice_answers = []
            match._choice_cursor = 0

    if target_out is not None:
        target_out["target"] = target
    if not finish:
        # A prepare-only run (#36): its output goes out, nothing else.
        if is_top_level:
            flush = match._runtime_buffer
            match._runtime_buffer = None
            if flush is not None and flush.out:
                await ctx.send("\n".join(str(x) for x in flush.out))
        return True, None
    # The action's say_format line (#215), about the actor.
    if action.say_format.strip():
        match.say(say_format_text(match, action.say_format, actor_id, target,
                                  dict(args), body_locals),
                  about=actor_id if (actor_id in match.entities or
                                     match.find_corpse(actor_id)) else "")
    # Successful completion: the holder has now used this action
    # (entity_description_action_visibility `used`). Recorded before the
    # hooks, so a hook that kills or removes the holder can't skip it; an
    # enclosing action that fails rolls it back with everything else.
    holder = match.entities.get(action.holder) if action.holder else None
    if holder is not None:
        for team in seers:
            paths = holder.actions_seen.setdefault(team, [])
            if action.full_path not in paths \
                    and len(paths) < ACTIONS_USED_MAX:
                paths.append(action.full_path)
                holder.mark_changed()
    # Successful completion. Fire the on_action_used hook AFTER the
    # body's writes have settled (so a passive observing this hook
    # sees post-action state).
    match.fire_action_used(
        action_tags=action.tags,
        actor_id=actor_id,
        action_name=action.name,
        action_path=action.full_path,
        target=target,
        args=dict(args),
    )
    match.log_event(
        "action_used", actor=actor_id,
        actor_name=match._entity_name(actor_id),
        action=action.name, action_path=action.full_path,
        target=_log_target_str(target),
    )
    # Target-side hook: fires on each ENTITY target after the
    # actor-side hook. For target=entity, fires once on that target.
    # For target=entity_list, once per eid. For location /
    # location_list / none, doesn't fire — there's no entity target.
    # The hook gives reactive passives a place to live: "shield
    # reflects melee", "trap counters attacker", etc. Inside the
    # hook, `self` binds to the target (the entity being acted on),
    # `actor` binds to the entity that used the action.
    target_eids: List[str] = []
    if action.target_type == "entity" and isinstance(target, str):
        if target in match.entities:
            target_eids.append(target)
    elif action.target_type == "entity_list" and isinstance(target, list):
        target_eids = [t for t in target if isinstance(t, str) and t in match.entities]
    for tid in target_eids:
        match.fire_action_used_on_target(
            action_tags=action.tags,
            target_id=tid,
            actor_id=actor_id,
            action_name=action.name,
            action_path=action.full_path,
            args=dict(args),
        )
    # Flush buffered command output through the real (awaitable) reply
    # context. Only the TOP-LEVEL action flushes — nested sub-actions
    # accumulated into the same shared buffer. This runs in async land
    # (run_action is a coroutine) so awaiting the real network send()
    # is legal here, even though the cmd() calls that produced these
    # lines ran under the synchronous formula engine.
    if is_top_level:
        flush = match._runtime_buffer
        match._runtime_buffer = None
        if flush is not None and flush.out:
            # One reply for the whole action (#215): on Discord each send
            # is a message, and an attack's say() lines read as one.
            await ctx.send("\n".join(str(x) for x in flush.out))
    return True, None


_SAY_FIELD_RE = re.compile(r"\{([A-Za-z_]\w*(?:\.[\w]+)*)\}")


def say_format_text(match: "Match", fmt: str, actor_id: str, target: Any,
                    args: Dict[str, Any], body_locals: Dict[str, Any]) -> str:
    """An action's say_format with its placeholders filled (#215): the
    body's locals, `self` (the actor), `target`, `args.<key>`. A unit (or
    corpse) id prints as a say name marker, which each channel's view reads
    as it sees the unit; `.name` the same; any other `.<path>` is the var,
    through the command's reader (a private var prints private_var_text).
    A list prints each item, joined with commas; an unknown placeholder
    prints nothing."""
    from logic import VAR_READER, privacy_spec, path_privacy, reader_sees
    hidden_text = str(match.rules.get("private_var_text", "?"))

    def is_unit(v: Any) -> bool:
        return isinstance(v, str) and (v in match.entities
                                       or match.find_corpse(v) is not None)

    def unit_path(eid: str, path: str) -> str:
        if path in ("", "name"):
            return match.say_name(eid)
        e = match.entities.get(eid)
        if e is None:
            loc = match.find_corpse(eid)
            uvars = (loc[2] or {}).get("vars", {}) if loc else {}
            if path == "x" and loc:
                return str(loc[0])
            if path == "y" and loc:
                return str(loc[1])
        else:
            if path in ("x", "y"):
                return str(getattr(e, path))
            uvars = e.vars
            reader = VAR_READER.get()
            if reader is not None:
                spec = privacy_spec(match.rules, uvars)
                team = match.body_team(e)
                if not reader_sees(path_privacy(spec, uvars, path), reader,
                                   None if team in (None, "") else str(team)):
                    return hidden_text
        cur: Any = uvars
        for seg in path.split("."):
            if not isinstance(cur, dict) or seg not in cur:
                return ""
            cur = cur[seg]
        return text_of(cur)

    def text_of(v: Any) -> str:
        if is_unit(v):
            return match.say_name(v)
        if isinstance(v, Coord):
            return f"({v.x}, {v.y})"
        if isinstance(v, (list, tuple)):
            return ", ".join(text_of(x) for x in v)
        if v is None:
            return ""
        return str(v)

    def value(key: str) -> str:
        head, _, rest = key.partition(".")
        if head == "self":
            base: Any = actor_id
        elif head == "target":
            base = target
        elif head == "args":
            if not rest:
                return ""
            cur: Any = args
            for seg in rest.split("."):
                if not isinstance(cur, dict) or seg not in cur:
                    return ""
                cur = cur[seg]
            return text_of(cur)
        elif head in body_locals:
            base = body_locals[head]
        else:
            return ""
        if not rest:
            return text_of(base)
        if isinstance(base, Coord):
            return str(getattr(base, rest, "")) if rest in ("x", "y", "map") else ""
        if isinstance(base, (list, tuple)):
            return ", ".join(unit_path(t, rest) if is_unit(t) else ""
                             for t in base)
        if is_unit(base):
            return unit_path(base, rest)
        cur = base
        for seg in rest.split("."):
            if not isinstance(cur, dict) or seg not in cur:
                return ""
            cur = cur[seg]
        return text_of(cur)

    return _SAY_FIELD_RE.sub(lambda mo: value(mo.group(1)), fmt)


def _restriction_refusal(action: Action, actor_id: str, target: Any,
                         match: "Match", extra_ctx: Optional[Dict[str, Any]],
                         ) -> Optional[Tuple[str, str, Any, List[str]]]:
    """None when no status restricts the action; else (reason, message,
    target, notes): reason `forbidden` / `immune` with the refusal, or ""
    with the entity_list target minus its immune units and the notes naming
    them."""
    from logic import ACTION_FORBID_FIELDS, ACTION_IMMUNE_FIELDS
    extra = extra_ctx or {}
    units = match.action_forbid_units(actor_id, extra.get("vehicle"),
                                      extra.get("rider"))
    hits = match.action_restrictions(ACTION_FORBID_FIELDS, units, action.name,
                                     action.tags, actor_id)
    if hits:
        return ("forbidden", "\n".join(
            match.action_restriction_line(
                "action_forbidden_message_format", h, action.name,
                action.tags, actor_id) for h in hits), target, [])

    def immune_lines(tid: str) -> List[str]:
        return [match.action_restriction_line(
                    "action_immune_message_format", h, action.name,
                    action.tags, actor_id, tid)
                for h in match.action_restrictions(
                    ACTION_IMMUNE_FIELDS, match.action_immune_units(tid),
                    action.name, action.tags, actor_id, tid)]

    corpse_target = action.target_type in ("corpse", "corpse_list")

    def refusals(tid: str) -> Tuple[str, List[str]]:
        # (reason, lines): immunity first (units only), then the target tag
        # filters (units and corpses).
        if not corpse_target:
            lines = immune_lines(tid)
            if lines:
                return "immune", lines
        lines = _target_tag_lines(action, actor_id, tid, match,
                                  corpse_target)
        return ("target_tags", lines) if lines else ("", [])

    if action.target_type in ("entity", "corpse") and isinstance(target, str):
        reason, lines = refusals(target)
        if lines:
            return reason, "\n".join(lines), target, []
    elif action.target_type in ("entity_list", "corpse_list") \
            and isinstance(target, list):
        kept: List[Any] = []
        dropped: List[str] = []
        reasons: List[str] = []
        for tid in target:
            reason, lines = refusals(tid) if isinstance(tid, str) \
                else ("", [])
            if lines:
                dropped.extend(lines)
                reasons.append(reason)
            else:
                kept.append(tid)
        if dropped and not kept:
            return reasons[0], "\n".join(dropped), target, []
        if dropped:
            return "", "", kept, [
                f"⚠️ `{action.name}` dropped a target: {ln}" for ln in dropped]
    return None


def target_condition_ok(action: Action, actor_id: str, tid: str,
                        match: "Match") -> Tuple[bool, str]:
    """Whether target `tid` meets the action's target_condition (#36;
    `self` = the actor, `target` = the target), and the error text when
    the formula fails (a failing formula is not met)."""
    if not action.target_condition:
        return True, ""
    from formula import FormulaEngine, EvalCtx
    try:
        ok = FormulaEngine(match).eval_expression(
            action.target_condition,
            EvalCtx(this=match.current_entity_id(), target=actor_id,
                    extras={"target": tid, "actor": actor_id,
                            "action_name": action.name,
                            "action_tags": list(action.tags)}))
        return bool(ok), ""
    except Exception as ex:      # noqa: BLE001 - reported, not raised
        return False, str(ex)


def _target_condition_refusal(action: Action, actor_id: str, target: Any,
                              match: "Match",
                              ) -> Optional[Tuple[str, str, Any, List[str]]]:
    """Like _restriction_refusal, for the action's target_condition: a
    single target that fails refuses the action; a list drops the ones
    that fail, refused only when none is left."""
    if action.target_type not in ("entity", "entity_list"):
        return None

    def line(tid: str, err: str) -> str:
        return (f"`{tid}` doesn't meet `{action.name}`'s target condition"
                + (f" (its formula failed: {err})" if err else "") + ".")
    if action.target_type == "entity" and isinstance(target, str):
        ok, err = target_condition_ok(action, actor_id, target, match)
        return None if ok else ("target_condition", line(target, err),
                                target, [])
    if isinstance(target, list):
        kept, dropped = [], []
        for tid in target:
            ok, err = target_condition_ok(action, actor_id, tid, match)
            (kept if ok else dropped).append((tid, err))
        if not dropped:
            return None
        if not kept:
            return ("target_condition",
                    "\n".join(line(t, e) for t, e in dropped), target, [])
        return "", "", [t for t, _e in kept], [
            f"⚠️ `{action.name}` dropped a target: {line(t, e)}"
            for t, e in dropped]
    return None


def _finish_body_less(action: Action, match: "Match", depth: int,
                      top: bool) -> Tuple[bool, Any]:
    """A reaction without a body of its own used as an action: refused."""
    match._action_depth = depth
    if top:
        match._runtime_buffer = None
    return False, ("reaction", f"`{action.name}` is a reaction: it runs "
                   f"through `!react` while a reaction window offers it.")


def _target_tag_lines(action: Action, actor_id: str, tid: str,
                      match: "Match", corpse: bool) -> List[str]:
    """Why target `tid` (a unit, or a corpse id) fails the action's
    target_requires_tags / target_refuses_tags filters, worded by
    action_target_tags_message_format; [] when it fits."""
    if action.target_requires_tags is None and \
            action.target_refuses_tags is None:
        return []
    if corpse:
        loc = match.find_corpse(tid)
        tags = match.corpse_tags(loc[2]) if loc and isinstance(loc[2], dict) \
            else []
    else:
        e = match.entities.get(tid)
        if e is None:
            return []
        tags = match.unit_tags(e)
    # The refusal names the tags the caller's view sees (a disguise's
    # decoys, V5++); the filters judge the real ones.
    seen = tags if corpse else match.unit_tags(e, match.command_pov())
    out: List[str] = []
    for key, want in (("target_requires_tags", True),
                      ("target_refuses_tags", False)):
        filt = getattr(action, key)
        if filt is None:
            continue
        try:
            ok = tag_filter_matches(tags, filt) == want
            err = None
        except VTTError as ex:
            ok, err = False, str(ex)
        if ok:
            continue
        shown = (", ".join(f"`{t}`" for t in filt) if isinstance(filt, list)
                 else f"`{filt}`")
        if len(shown) > 80:
            # A stored filter can run to a thousand characters (L23).
            shown = shown[:77] + "..."
        fmt = str(match.rules.get("action_target_tags_message_format") or "") \
            or RULES_REGISTRY["action_target_tags_message_format"]["default"]
        fixed = {"target": tid, "actor": actor_id, "action": action.name,
                 "field": key, "filter": shown,
                 "target_tags": ", ".join(seen) or "none",
                 "fails": (f"don't match {shown}" if want
                           else f"match {shown}")}
        text = match._MSG_FMT_RE.sub(
            lambda mo: str(fixed.get(mo.group(1), mo.group(0))), fmt)
        if err:
            text += f" (its `{key}` doesn't parse: {err})"
        out.append(text)
    return out


_ROLLBACK_FIELDS_CHECKED = False


def _check_rollback_fields(preserved_attrs) -> None:
    """Drift guard, run once: every Match field the rollback pre-state does
    NOT carry must be in `preserved_attrs`, or a rollback silently resets it
    (this list went stale twice: the event-bus fields, then `history`)."""
    global _ROLLBACK_FIELDS_CHECKED
    if _ROLLBACK_FIELDS_CHECKED:
        return
    import dataclasses
    from logic import Match, default_maps
    probe = Match(id="_probe", name="_probe", maps=default_maps(1, 1),
                  system_name="default")
    carried = probe.to_dict(include_history=False)
    missing = [f.name for f in dataclasses.fields(Match)
               if f.name not in carried and f.name not in preserved_attrs]
    if missing:
        raise RuntimeError(
            f"action._rollback_match: Match field(s) {missing} are neither "
            f"serialized nor in preserved_attrs — a rollback would reset "
            f"them. Add them to preserved_attrs (runtime state) or to "
            f"Match.to_dict (game state).")
    _ROLLBACK_FIELDS_CHECKED = True


def _snapshot_other_matches(mgr: Any, match: "Match") -> Dict[str, Any]:
    """Every match of the server but `match`, serialized, and the channel
    pointers: what an action's cmd() line may change elsewhere."""
    return {"matches": {mid: m.to_dict(include_history=False)
                        for mid, m in mgr.matches.items() if m is not match},
            "objs": {mid: m for mid, m in mgr.matches.items()
                     if m is not match},
            "hist": {mid: m.history.mark() for mid, m in mgr.matches.items()
                     if m is not match},
            "active": dict(mgr.active_by_channel)}


def _restore_other_matches(mgr: Any, match: "Match",
                           snap: Dict[str, Any]) -> None:
    """Put the other matches back as `snap` (_snapshot_other_matches) holds
    them: a match made since goes, a deleted one returns, a changed one is
    rolled back in place (keeping its undo history); channel pointers too."""
    from logic import Match as _Match
    pre = snap["matches"]
    for mid in [mid for mid, m in mgr.matches.items()
                if m is not match and mid not in pre]:
        del mgr.matches[mid]
    for mid, state in pre.items():
        m = mgr.matches.get(mid)
        if m is None:
            m = snap["objs"].get(mid) or _Match.from_dict(copy.deepcopy(state))
            mgr.matches[mid] = m
            if not same_value(m.to_dict(include_history=False), state):
                _rollback_match(m, mgr, state)
        elif not same_value(m.to_dict(include_history=False), state):
            _rollback_match(m, mgr, state)
        if mid in snap["hist"]:
            m.history.rewind(snap["hist"][mid])
        if m.system_name in mgr.systems:
            mgr.apply_system_rules(m)
    mgr.active_by_channel.clear()
    mgr.active_by_channel.update(snap["active"])


def _rollback_match(match: "Match", mgr: Any, pre_state: Dict[str, Any]) -> None:
    """Restore a Match in-place to the given pre-state dict. Used by
    the action runner when the body raises. We swap the live Match
    object's contents rather than replacing the manager's pointer
    because the action runner is deeply nested in coroutines that
    hold references to the original instance; rebinding the
    manager's entry would leave them pointing at the post-rollback
    state through stale aliases.

    Implementation: build a fresh Match from pre_state (which
    captures vars, entities, rules, etc.), then copy its __dict__
    entries back into the live match. Runtime-only state (_rng,
    _action_depth, _var_event_depth) is preserved on the live match
    — those are by-design transient and shouldn't be reset by an
    action rollback."""
    from logic import Match  # local import
    restored = Match.from_dict(pre_state)
    # Preserve ALL runtime-only fields the snapshot doesn't carry: they are
    # by-design transient and must NOT be reset by an action rollback, which
    # only restores SERIALIZED state. Critically, several of these track an
    # operation that is STILL IN FLIGHT around the rollback — e.g. the event
    # bus stack/depth when an action runs inside an emitted handler (resetting
    # it crashes the outer emit's cleanup with "pop from empty list"), or the
    # per-command summon budget (resetting it would refill it mid-command).
    # This list must stay in sync with the runtime (underscore) fields declared
    # on Match; from_dict gives them defaults, so anything omitted here would be
    # clobbered. Only SERIALIZED state should come from `restored`.
    preserved_attrs = (
        "_rng", "_rng_seed",
        # The RNG keeps its live seed and position (not undo state).
        "rng_seed", "rng_pos",
        # The command journal records the match from outside its state.
        "journal",
        "_var_event_depth", "_var_event_warned", "_var_event_warnings",
        "_status_event_depth", "_status_event_warned",
        "_grant_syncing", "_grant_dirty", "_grant_changes",
        "_grant_warned",
        "_action_depth", "_runtime_buffer",
        # The mid-body choice queue + cursor must survive an action's
        # rollback-and-retry (the whole point of replay).
        "_choice_answers", "_choice_cursor",
        # In-flight operation guards/state that span a nested rollback.
        "_summon_count", "_death_processing", "_death_check_suppressed_ids",
        "_death_check_deferred_ids", "_deleting_maps", "_watcher_backlog",
        "_alive_eval_depth", "_vision_memo", "_spatial", "_uflags", "_size_touch", "_turn_order_dirty",
        "_request_seq", "pending_requests", "held_commands",
        # Wall clock of the open reaction window (expiry; runtime).
        "_reaction_opened",
        # Event bus: the live emit stack/depth + warning latch. Resetting
        # these mid-emit corrupts the outer emit (the verified crash).
        "_event_depth", "_event_stack", "_event_warned", "_event_warnings",
        # ATB latches (one-time dormant-round warning + last-actor-skipped).
        "_atb_round_warned", "_atb_last_skipped",
        # The undo history. The pre-state is taken WITHOUT history, so
        # from_dict builds an empty one; copying that over wiped every
        # autosave AND manual save on each failed action.
        "history",
        # Serialized, but only `!system rename` changes it, and a rollback
        # (a `!batch strict` that renamed a system, then failed) must not
        # point the match back at a name that no longer exists.
        "system_name",
    )
    _check_rollback_fields(preserved_attrs)
    preserved = {a: getattr(match, a) for a in preserved_attrs
                 if hasattr(match, a)}
    # Replace serialized state. We iterate a snapshot of restored's
    # __dict__ to avoid mutating-while-iterating shenanigans.
    for k, v in vars(restored).items():
        setattr(match, k, v)
    # Restore runtime-only state.
    for a, v in preserved.items():
        setattr(match, a, v)
    # Re-parent entities so their _match backref points at the live
    # match instance (Match.from_dict set them to `restored`).
    for e in match.entities.values():
        e._match = match
    match._spatial.reset()


def _sync_dispatch(coro) -> None:
    """Synchronously drive a coroutine to completion. Used by the
    action runner's cmd() so a sync formula evaluator can dispatch
    an async command. Works by repeatedly calling .send(None) until
    the coroutine raises StopIteration. The dispatcher's awaits are
    all ctx.send() calls (which we control — _Ctx.send in the
    scenario harness is a coroutine that completes immediately), so
    this finishes in one trip in practice. For non-trivial async
    contexts (the real Discord client), this would need a different
    bridge — but inside an action body run from an already-async
    handler, we know the only awaits are the trivial ones the
    command surface generates."""
    try:
        while True:
            coro.send(None)
    except StopIteration:
        return


def _sync_dispatch_returning(coro) -> Any:
    """Same as _sync_dispatch but returns the coroutine's return
    value (carried in StopIteration.value). Used by use_action to
    propagate the (success, message) tuple back to the formula."""
    try:
        while True:
            coro.send(None)
    except StopIteration as stop:
        return stop.value
