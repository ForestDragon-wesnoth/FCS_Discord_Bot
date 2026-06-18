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
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from logic import VTTError, NotFound

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

    def __init__(self, channel_key: str):
        self.channel_key = channel_key
        self.out: List[str] = []

    async def send(self, message: str) -> None:
        self.out.append(message)


# Reserved subkey under any vars container that holds the action
# dictionary. A container with a key named "actions" whose value is a
# dict has each child of that dict treated as an Action definition.
ACTIONS_KEY = "actions"

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

    def __init__(self, message: str, *, reason: str = ""):
        super().__init__(message)
        self.message = message
        self.reason = reason


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
    rng = getattr(match, "_rng", None)
    if rng is not None:
        state["match"] = rng.getstate()
    return state


def _restore_rng(match: "Match", state: Dict[str, Any]) -> None:
    import random
    random.setstate(state["global"])
    rng = getattr(match, "_rng", None)
    if rng is not None and "match" in state:
        rng.setstate(state["match"])


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

    @staticmethod
    def from_dict(
        name: str, raw: Any, *,
        container_path: str, full_path: str,
    ) -> "Action":
        """Build an Action from a vars subtree. Raises
        ActionValidationError if the shape is wrong."""
        if not isinstance(raw, dict):
            raise ActionValidationError(
                f"action '{name}' at `{full_path}` must be a dict "
                f"(got {type(raw).__name__})."
            )
        body = raw.get("body", "")
        if not isinstance(body, str) or not body.strip():
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
        return Action(
            name=name, body=body, description=description,
            target_type=target_type,
            container_path=container_path, full_path=full_path,
            allowed_slots=allowed_slots,
        )


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

    # Top-level intrinsic actions: entity.vars.actions.<name>. Bypass
    # the container scoping (it's the catch-all slot for entity-level
    # actions that don't belong to any item/skill/etc.).
    intrinsic = entity.vars.get(ACTIONS_KEY)
    if isinstance(intrinsic, dict):
        for name, raw in intrinsic.items():
            try:
                act = Action.from_dict(
                    name, raw, container_path="",
                    full_path=f"{ACTIONS_KEY}.{name}",
                )
            except ActionValidationError:
                continue
            out.setdefault(name, []).append(act)

    # Container actions. Each top-level key (subject to the container
    # rules) is the root of a recursive walk that looks for `.actions.`
    # subdicts at any depth. Note that the walker descends INTO each
    # container but skips the `actions` key when it finds one (no
    # actions-within-actions).
    for top_key, top_val in entity.vars.items():
        if top_key == ACTIONS_KEY:
            continue  # already handled above
        if not _top_container_allowed(top_key, rules):
            continue
        if not isinstance(top_val, dict):
            continue
        _walk_for_actions(
            top_val, base_path=top_key, out=out,
        )
    return out


def _walk_for_actions(
    container: Dict[str, Any], *,
    base_path: str, out: Dict[str, List[Action]],
) -> None:
    """Recursive helper for discover_actions. Walks `container` and:
      - if it has an `actions` key with a dict value, every child of
        that dict is an Action of `base_path`
      - for every OTHER child that's itself a dict, recurses with the
        extended path (so nested containers like
        `inventory.bags.left.sword` are reachable)
    Non-dict children are leaves and ignored. The `actions` subkey
    is NEVER recursed into."""
    actions_subtree = container.get(ACTIONS_KEY)
    if isinstance(actions_subtree, dict):
        for name, raw in actions_subtree.items():
            try:
                act = Action.from_dict(
                    name, raw,
                    container_path=base_path,
                    full_path=f"{base_path}.{ACTIONS_KEY}.{name}",
                )
            except ActionValidationError:
                continue
            out.setdefault(name, []).append(act)
    for k, v in container.items():
        if k == ACTIONS_KEY:
            continue
        if isinstance(v, dict):
            _walk_for_actions(
                v,
                base_path=f"{base_path}.{k}",
                out=out,
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
        self._entity.write_var(path, value)


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

    __slots__ = ("x", "y")

    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y

    def __repr__(self) -> str:
        return f"Coord({self.x}, {self.y})"

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
    """
    if target_type == "none":
        return None, list(tokens)
    if target_type == "entity":
        if not tokens:
            raise VTTError("expected an entity id after the action name.")
        eid_token = tokens[0]
        eid = _resolve_eid_for_action(eid_token, match)
        if eid not in match.entities:
            raise NotFound(f"Entity '{eid}' not found.")
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
        return Coord(x, y), list(tokens[2:])
    if target_type == "entity_list":
        eids: List[str] = []
        i = 0
        while i < len(tokens) and "=" not in tokens[i]:
            eid = _resolve_eid_for_action(tokens[i], match)
            if eid not in match.entities:
                raise NotFound(f"Entity '{eid}' not found.")
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
            coords.append(Coord(x, y))
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
        if match.find_corpse(cid) is None:
            raise NotFound(f"No corpse with id '{cid}'.")
        return cid, list(tokens[1:])
    if target_type == "corpse_list":
        cids: List[str] = []
        i = 0
        while i < len(tokens) and "=" not in tokens[i]:
            if match.find_corpse(tokens[i]) is None:
                raise NotFound(f"No corpse with id '{tokens[i]}'.")
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
        buffer_ctx = _BufferCtx(getattr(ctx, "channel_key", "CLI"))
        match._runtime_buffer = buffer_ctx
    else:
        buffer_ctx = getattr(match, "_runtime_buffer", None)
        if buffer_ctx is None:
            buffer_ctx = _BufferCtx(getattr(ctx, "channel_key", "CLI"))
            match._runtime_buffer = buffer_ctx

    # Build the action's eval bindings. SourceProxy/ArgsProxy
    # implement the body-side magic; cmd/fail are closure-built so
    # they can see this runner's match + buffer + allowlist.
    source = SourceProxy(actor, action.container_path)
    args_proxy = ArgsProxy(args)
    cmd_allowlist = parse_cmd_allowlist(match.rules)

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
        import shlex
        from vtt_commands import registry  # noqa: PLC0415
        try:
            tokens = shlex.split(line)
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
        coro = registry.dispatch_no_snapshot(
            tokens[0], tokens[1:], match._runtime_buffer or buffer_ctx, mgr,
        )
        _sync_dispatch(coro)

    def _fail(*fail_args: Any) -> None:
        """fail(message) | fail(reason, message): abort the action
        body cleanly. The runner catches ActionFail, rolls back to
        pre-state, fires on_action_failed (when reachable), and
        surfaces `❌ <action>: [<reason>] <message>` (the [reason]
        prefix is dropped when reason is empty). Always raises —
        control does NOT return to the body.

        Reason is a GM-chosen tag like "cost", "out_of_range",
        "invalid_target". It's surfaced to on_action_failed passives
        via the `fail_reason` binding so reactive logic can
        discriminate between failure modes."""
        if len(fail_args) == 1:
            reason = ""
            message = str(fail_args[0])
        elif len(fail_args) == 2:
            reason = str(fail_args[0])
            message = str(fail_args[1])
        else:
            raise ActionFail(
                "fail() takes 1 or 2 arguments: fail(message) or "
                "fail(reason, message).",
                reason="usage",
            )
        raise ActionFail(message, reason=reason)

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

    bindings = {
        "source": source,
        "args": args_proxy,
        "target": target,
        "cmd": _cmd,
        "fail": _fail,
        "choose": _choose,
        "choose_number": _choose_number,
    }

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

    match._action_depth += 1
    try:
        engine = FormulaEngine(match)
        eval_ctx = EvalCtx(
            this=match.current_entity_id(),
            target=actor_id,
            extras=dict(extra_ctx) if extra_ctx else None,
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
            choice_limit = int(match.rules.get("action_choice_limit", 20))
        while True:
            if is_top_level:
                match._choice_cursor = 0
                _restore_rng(match, rng_state)
                # Fresh output buffer per attempt — rolled-back attempts'
                # command echoes must not leak into the committed run.
                match._runtime_buffer = _BufferCtx(getattr(ctx, "channel_key", "CLI"))
            try:
                engine.eval_program(
                    action.body, eval_ctx,
                    action_mode=True,
                    action_bindings=bindings,
                )
            except ChoiceNeeded as nc:
                if not is_top_level:
                    raise  # the top-level loop owns replay
                _rollback_match(match, mgr, pre_state)
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
                except ActionFail as af:
                    return _finish_action_fail(af)
                match._choice_answers.append(ans)
                continue
            except ActionFail as af:
                # Clean GM-initiated abort. Roll back, then finish.
                # Buffered command echoes are DISCARDED — the action
                # rolled back, so its partial output would mislead.
                _rollback_match(match, mgr, pre_state)
                return _finish_action_fail(af)
            except ActionEngineFault:
                # Engine refusal (recursion / choice limit). Roll back
                # AND re-raise so every level unwinds.
                _rollback_match(match, mgr, pre_state)
                match._action_depth = pre_action_depth
                if is_top_level:
                    match._runtime_buffer = None
                raise
            except Exception:
                _rollback_match(match, mgr, pre_state)
                match._action_depth = pre_action_depth
                if is_top_level:
                    match._runtime_buffer = None
                raise
            else:
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

    # Successful completion. Fire the on_action_used hook AFTER the
    # body's writes have settled (so a passive observing this hook
    # sees post-action state).
    match.fire_action_used(
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
        if flush is not None:
            for line in flush.out:
                await ctx.send(line)
    return True, None


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
    # Preserve runtime-only fields the snapshot doesn't carry. Names
    # listed here match the field defaults declared on Match for
    # runtime-only state.
    preserved_attrs = (
        "_rng", "_rng_seed",
        "_var_event_depth", "_var_event_warned", "_var_event_warnings",
        "_action_depth", "_runtime_buffer",
        # The mid-body choice queue + cursor must survive an action's
        # rollback-and-retry (the whole point of replay).
        "_choice_answers", "_choice_cursor",
    )
    preserved = {a: getattr(match, a) for a in preserved_attrs}
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
