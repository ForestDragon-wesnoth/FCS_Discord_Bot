"""
formula.py — Formula engine for the VTT.

================================================================================
OVERVIEW
================================================================================
Formulas are small, sandboxed snippets of Python-like code that can read and
write entity variables. They power:
  - $("...") substitution in command arguments (e.g. !ent hp hero "$(...)")
  - The !eval command, for ad-hoc inspection
  - The body of every passive — both entity-scoped and match-scoped global

Two evaluation modes are exposed:
  - eval_expression(src, ctx) -> value
      Parses src as a single expression. Used for $(...) substitution.
  - eval_program(src, ctx)    -> value | None
      Parses src as a sequence of statements. If the final statement is an
      expression, its value is returned; otherwise returns None. This is what
      passive formulas use.

Validation also has a public helper: validate_formula(src, mode="exec"), used
to catch malformed formulas at registration time rather than at fire time.

================================================================================
VARIABLE ACCESS
================================================================================
The only way to touch entity state is the magic `entity[X].path` form:

  entity[hero].hp                  reads `vars["hp"]` of entity with id "hero"
  entity[hero].inventory.sword     reads `vars["inventory"]["sword"]`
  entity[hero].hp = 30             writes 30 to `vars["hp"]`
  entity["rogue"].hp               same as entity[rogue].hp (string-literal id)

The X inside the brackets may be:
  - A bare identifier:      entity[hero]
  - A string literal:       entity["hero"]
  - One of two specials:
      entity[this]    or entity[current]   -> the entity whose turn it is now
      entity[self]                          -> the entity bound by the current
                                               frame (the passive's owner, or
                                               the target of a command). In an
                                               !eval call `self` is unbound.

Reading a path that does not exist raises FormulaError. There are no silent
defaults. If you need a default, write it explicitly: e.g.
  entity[self].hp = entity[self].hp + 5
will fail loudly if `hp` was never set — which is usually what you want.

Writing creates intermediate dicts as needed:
  entity[hero].inventory.bow.damage = 8
will produce `vars["inventory"] = {"bow": {"damage": 8}}` even if `inventory`
didn't previously exist.

================================================================================
OPERATORS AND BUILT-INS
================================================================================
Arithmetic:        +  -  *  /  //  %  **       (binary)
Unary:             +x   -x   not x
Comparison:        ==  !=  <  <=  >  >=
Boolean:           and  or  not
Ternary:           value_if_true if cond else value_if_false

Allowed functions: min, max, abs, round, int, float, str

Everything else is rejected: list comprehensions, lambdas, augmented
assignment (`+=`), subscripting (other than `entity[X]`), attribute access on
non-entity values, imports, function definitions, decorators, exception
handling, loops, with-blocks, etc.

================================================================================
CONDITIONAL FLOW
================================================================================
Two forms of conditional are supported.

(1) Ternary expressions — preferred when picking a single value:

  entity[self].hp = entity[self].hp + (5 if entity[self].resting == "yes" else 2)

(2) if / elif / else statements — preferred when whole assignments differ:

  if entity[self].resting == "yes":
      entity[self].stamina = (entity[self].stamina + 10) * 2
  elif entity[self].resting == "light":
      entity[self].stamina = entity[self].stamina + 6
  else:
      entity[self].stamina = entity[self].stamina + 2

Both forms compose freely: ternaries can appear inside if-bodies, conditions
can be compound boolean expressions, ifs can nest:

  if entity[self].hp <= 0 and entity[self].revives > 0:
      entity[self].hp = entity[self].max_hp // 2
      entity[self].revives = entity[self].revives - 1

`elif` is just Python's `elif` — chain as many as needed. `else` is optional.
An `if` without an `else` simply does nothing on the false branch.

There is NO match/case statement; use an elif chain. There are NO local
variables; if you need an intermediate value, recompute it or use a ternary
inside a single assignment.

================================================================================
TIPS AND COMMON PATTERNS
================================================================================
Clamp a value within bounds:
  entity[self].hp = min(max(entity[self].hp + 5, 0), entity[self].max_hp)

Conditional damage based on status:
  if entity[self].defending == "yes":
      entity[self].hp = entity[self].hp - max(entity[attacker].atk - 5, 0)
  else:
      entity[self].hp = entity[self].hp - entity[attacker].atk

Conditional self-heal in a passive (on_turn_start):
  if entity[self].hp < entity[self].max_hp:
      entity[self].hp = min(entity[self].hp + 3, entity[self].max_hp)

Multi-step assignment cascade:
  entity[self].turns_alive = entity[self].turns_alive + 1
  if entity[self].turns_alive >= 5:
      entity[self].state = "veteran"

Ternary inside a $() substitution token (note the outer quoting at the
command layer — required so the shell doesn't split on spaces):
  !ent hp hero "$(entity[self].max_hp if entity[self].blessed else entity[self].hp + 5)"

================================================================================
SECURITY NOTES
================================================================================
Formulas run with __builtins__ disabled and a curated namespace. The AST is
walked and every node is checked against an explicit whitelist before any
code is compiled. Identifiers are restricted to the small set above plus
the internal __read/__write helpers injected by the transformer. There is
no path to import modules, open files, or escape the sandbox through normal
formula source. (Defensive note: a buggy formula that raises at runtime is
caught by the passive runner and reported as a "CRASHED" log line; sibling
passives still fire.)
"""
from __future__ import annotations
import ast
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from logic import VTTError


class FormulaError(VTTError):
    """Raised when a formula cannot be parsed or evaluated."""
    pass


# --- whitelists --------------------------------------------------------------

_ALLOWED_FUNCS: Dict[str, Any] = {
    "min": min, "max": max, "abs": abs, "round": round,
    "int": int, "float": float, "str": str,
}

_ALLOWED_NODES: Tuple[type, ...] = (
    ast.Module, ast.Expression,
    ast.Expr, ast.Assign,
    # Control flow:
    #   If         -> if / elif / else statements (elif is encoded as else: If)
    #   Pass       -> empty bodies, e.g. `if cond: pass`
    #   IfExp      -> ternary  (value_a if cond else value_b)
    ast.If, ast.Pass,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.IfExp, ast.Compare,
    ast.Call, ast.keyword,
    ast.Name, ast.Constant, ast.Load, ast.Store,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.And, ast.Or,
)


# --- evaluation context ------------------------------------------------------

@dataclass
class EvalCtx:
    """Bindings for special who-references during formula evaluation."""
    this: Optional[str] = None       # entity whose turn it currently is
    target: Optional[str] = None     # what 'self' resolves to

    def resolve_who(self, token: str) -> str:
        if token in ("this", "current"):
            if not self.this:
                raise FormulaError(
                    f"'{token}' is unbound (no current entity in turn order)."
                )
            return self.this
        if token == "self":
            if not self.target:
                raise FormulaError("'self' is unbound in this context.")
            return self.target
        return token  # literal entity id


# --- AST flattening / rewriting ---------------------------------------------

def _who_from_slice(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise FormulaError(
        'entity[...] index must be a bare identifier (entity[rogue]) '
        'or a string literal (entity["rogue"]).'
    )


def _flatten_entity_chain(node: ast.AST) -> Optional[Tuple[str, List[str]]]:
    """
    Recognize chains of the form entity[X].a.b.c... and return
    (who_token, ['a', 'b', 'c', ...]). Returns None if `node` is not such a chain.
    """
    parts: List[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if (isinstance(cur, ast.Subscript)
            and isinstance(cur.value, ast.Name)
            and cur.value.id == "entity"):
        slice_node = cur.slice
        if isinstance(slice_node, ast.Index):  # py<=3.8 wrapper, defensive
            slice_node = slice_node.value
        who = _who_from_slice(slice_node)
        parts.reverse()
        return who, parts
    return None


class _EntityAccessTransformer(ast.NodeTransformer):
    """Rewrites entity[X].path reads/writes into __read/__write helper calls."""

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        # Don't recurse: the whole chain is handled at once by _flatten.
        match = _flatten_entity_chain(node)
        if match is None:
            raise FormulaError("Attribute access is only allowed on entity[X].path.")
        who, parts = match
        if not parts:
            raise FormulaError("entity[X] must be followed by .path.")
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="__read", ctx=ast.Load()),
                args=[ast.Constant(value=who),
                      ast.Constant(value=".".join(parts))],
                keywords=[],
            ),
            node,
        )

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        # Transform RHS (may itself contain entity[X].path reads).
        node.value = self.visit(node.value)
        if len(node.targets) != 1:
            raise FormulaError("Chained / tuple assignment is not supported.")
        match = _flatten_entity_chain(node.targets[0])
        if match is None:
            raise FormulaError("Assignment target must be entity[X].path.")
        who, parts = match
        if not parts:
            raise FormulaError("entity[X] must be followed by .path.")
        call = ast.Call(
            func=ast.Name(id="__write", ctx=ast.Load()),
            args=[ast.Constant(value=who),
                  ast.Constant(value=".".join(parts)),
                  node.value],
            keywords=[],
        )
        return ast.copy_location(ast.Expr(value=call), node)

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
        raise FormulaError(
            "Augmented assignment (+= etc.) is not supported; "
            "use 'entity[X].var = entity[X].var + amount' instead."
        )

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        # Bare entity[X] without .attribute, or any other subscripting.
        if isinstance(node.value, ast.Name) and node.value.id == "entity":
            raise FormulaError("entity[X] must be followed by .path.")
        raise FormulaError("Subscripting is not allowed (only entity[X].path).")


def _validate_tree(tree: ast.AST) -> None:
    for n in ast.walk(tree):
        if not isinstance(n, _ALLOWED_NODES):
            raise FormulaError(f"Disallowed syntax: {type(n).__name__}")
        if isinstance(n, ast.Name):
            if n.id not in ("__read", "__write") and n.id not in _ALLOWED_FUNCS:
                raise FormulaError(f"Unknown identifier '{n.id}'.")
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name):
                raise FormulaError("Only direct function calls are allowed.")
            fname = n.func.id
            if fname not in ("__read", "__write") and fname not in _ALLOWED_FUNCS:
                raise FormulaError(f"Function '{fname}' is not allowed.")


# --- variable-path helpers ---------------------------------------------------

def _get_path(d: Dict[str, Any], path: str) -> Any:
    keys = path.split(".")
    cur: Any = d
    for i, k in enumerate(keys):
        if not isinstance(cur, dict):
            raise FormulaError(
                f"Cannot read '{path}': '{'.'.join(keys[:i])}' is not a dict."
            )
        if k not in cur:
            raise FormulaError(f"Variable '{path}' is not defined.")
        cur = cur[k]
    return cur


def _set_path(d: Dict[str, Any], path: str, value: Any) -> None:
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        node = cur.get(k)
        if not isinstance(node, dict):
            node = {}
            cur[k] = node
        cur = node
    cur[keys[-1]] = value


def validate_formula(src: str, *, mode: str = "exec") -> None:
    """Parse, transform, and validate a formula source string.

    Raises FormulaError on any syntactic or semantic problem. Useful for
    failing early when a passive is registered, rather than at hook-fire time.

    mode: 'exec' for full program bodies (assignments and expressions),
          'eval' for pure expressions.
    """
    FormulaEngine._prepare(src, mode)


# --- engine ------------------------------------------------------------------

class FormulaEngine:
    """Parses and evaluates formulas against a Match."""

    def __init__(self, match):
        self._match = match
    def _read(self, who: str, path: str, ctx: EvalCtx) -> Any:
        eid = ctx.resolve_who(who)
        e = self._match.entities.get(eid)
        if e is None:
            raise FormulaError(f"Entity '{eid}' not found.")
        return _get_path(e.vars, path)

    def _write(self, who: str, path: str, value: Any, ctx: EvalCtx) -> Any:
        eid = ctx.resolve_who(who)
        e = self._match.entities.get(eid)
        if e is None:
            raise FormulaError(f"Entity '{eid}' not found.")
        _set_path(e.vars, path, value)
        return value

    def _namespace(self, ctx: EvalCtx) -> Dict[str, Any]:
        return {
            "__read":  lambda who, path:        self._read(who, path, ctx),
            "__write": lambda who, path, value: self._write(who, path, value, ctx),
            **_ALLOWED_FUNCS,
        }

    @staticmethod
    def _prepare(src: str, mode: str) -> ast.AST:
        try:
            tree = ast.parse(src, mode=mode)
        except SyntaxError as e:
            raise FormulaError(f"Syntax error: {e.msg}")
        tree = _EntityAccessTransformer().visit(tree)
        ast.fix_missing_locations(tree)
        _validate_tree(tree)
        return tree

    def eval_expression(self, src: str, ctx: EvalCtx) -> Any:
        tree = self._prepare(src, "eval")
        code = compile(tree, "<formula>", "eval")
        try:
            return eval(code, {"__builtins__": {}}, self._namespace(ctx))
        except FormulaError:
            raise
        except Exception as e:
            raise FormulaError(f"Runtime error: {e}")

    def eval_program(self, src: str, ctx: EvalCtx) -> Any:
        """Run statements; if the source ends with an expression, return its value."""
        try:
            full = ast.parse(src, mode="exec")
        except SyntaxError as e:
            raise FormulaError(f"Syntax error: {e.msg}")

        trailing_expr = None
        if full.body and isinstance(full.body[-1], ast.Expr):
            trailing_expr = full.body.pop().value

        full = _EntityAccessTransformer().visit(full)
        ast.fix_missing_locations(full)
        _validate_tree(full)

        ns = self._namespace(ctx)
        try:
            if full.body:
                exec(compile(full, "<formula>", "exec"), {"__builtins__": {}}, ns)
            if trailing_expr is None:
                return None
            expr_tree = ast.Expression(body=trailing_expr)
            expr_tree = _EntityAccessTransformer().visit(expr_tree)
            ast.fix_missing_locations(expr_tree)
            _validate_tree(expr_tree)
            return eval(compile(expr_tree, "<formula>", "eval"),
                        {"__builtins__": {}}, ns)
        except FormulaError:
            raise
        except Exception as e:
            raise FormulaError(f"Runtime error: {e}")


# --- arg-token resolution ($(...) substitution) -----------------------------

_QUOTED_RE = re.compile(r'^\s*\$\(\s*"(?P<body>.*)"\s*\)\s*$', flags=re.DOTALL)
_BARE_RE   = re.compile(r'^\s*\$\((?P<body>.*)\)\s*$',           flags=re.DOTALL)


def resolve_arg_token(token: str, match, self_id: Optional[str] = None) -> str:
    """
    If `token` is a $("...") or $(...) formula, evaluate it as an expression
    against `match` (with `this` = current-turn entity, `self` = self_id) and
    return the stringified result. Otherwise return `token` unchanged.
    """
    if not isinstance(token, str) or not token.startswith("$("):
        return token
    m = _QUOTED_RE.match(token) or _BARE_RE.match(token)
    if m is None:
        raise FormulaError(f"Malformed formula token: {token!r}")
    this_id = match.current_entity_id() if hasattr(match, "current_entity_id") else None
    ctx = EvalCtx(this=this_id, target=self_id)
    val = FormulaEngine(match).eval_expression(m.group("body"), ctx)
    return _stringify(val)


def _stringify(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if v is None:
        return ""
    return str(v)


# --- public validation helper -----------------------------------------------

def validate_program(src: str) -> None:
    """
    Parse and validate a formula in program mode (statements + optional
    trailing expression). Raises FormulaError on syntax errors or disallowed
    constructs. Used to eagerly catch broken passive formulas at add time.
    Does not need a match — purely a syntax check.
    """
    FormulaEngine._prepare(src, "exec")