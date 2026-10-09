"""run_scenarios.py — execute the scenarios in test_sequences.txt.

test_sequences.txt is the project's living integration-test document:
each SCENARIO block is a sequence of `!command` lines followed by a
freeform "Expected:" prose description. The prose isn't machine-checked
(that would mean restructuring every scenario), but RUNNING every
command catches the failures that matter most:

  - 💥  an unexpected Python exception escaped a command handler
        (always a bug)
  - "Syntax error: ..."  a formula failed to PARSE. In a well-formed
        scenario this never happens — the sandbox's *intentional*
        rejections surface as "Disallowed syntax: X", "Unknown
        identifier", "Function not allowed", etc., never as a Python
        "Syntax error". So a "Syntax error" line means the scenario's
        command is malformed (the historical cause: multi-line formulas
        written with literal `\n` that nothing un-escaped).

Multi-line formulas: scenarios write newlines inside a command as the
two-character escape `\n` (and tabs as `\t`) so the whole command stays
on one physical line — the SCENARIO parser is line-oriented. The real
bot receives actual newlines (Discord message content / CLI stdin), so
this runner converts `\n`/`\t` back to real characters BEFORE shlex,
exactly reproducing what the command layer would see.

Reply expectations: a line `?? <text>` right after a command checks that
the command's reply CONTAINS <text>; `?! <text>` checks that it does NOT.
They run in order with the commands (above the Expected: prose) and a miss
prints `❌ Expectation failed`, which is always flagged — the way to check
wording and refusal messages, which `!assert` (formulas only) can't see.
Surrounding double quotes on <text> are stripped, so `?? "  spaced "` keeps
its inner spaces.

Usage:
    python run_scenarios.py                 # run all, summarize failures
    python run_scenarios.py -v              # also print a transcript
    python run_scenarios.py 244 245         # run only these scenarios
    python run_scenarios.py --list          # list scenario numbers/titles
    python run_scenarios.py --review 613    # full replies next to the prose

Exit code is nonzero if any scenario produced a flagged failure.
"""
from __future__ import annotations
import argparse
import asyncio
import os
import re
import shlex
import sys
from typing import Dict, List, Tuple

from logic import Workspaces, LOCAL_WORKSPACE
from vtt_commands import registry

SCENARIO_RE = re.compile(
    r"\nSCENARIO (\d+)\s*[—-]\s*([^\n]*)\n[-]+\n(.*?)(?=\n\nSCENARIO |\Z)",
    re.DOTALL,
)

# Output substrings that ALWAYS mark a flagged failure. 💥 is the dispatcher's
# unexpected-exception prefix; "Syntax error" is a formula parse failure
# (see module docstring for why that's always a malformed scenario).
_FAILURE_MARKERS = ("💥", "Syntax error")

# Substrings that mark a top-level command error (a caught VTTError/FormulaError
# surfaced as a ❌ reply). In a WELL-FORMED scenario these are bugs — a core
# feature silently returning ❌ is exactly how the summon regression rotted
# undetected across eleven of its own tests. They're flagged UNLESS a scenario
# deliberately exercises error handling (a func-deletion test, a rejection
# test) and opts out with the `HARNESS-ALLOWS-ERRORS` tag in its Expected prose.
_ERROR_MARKERS = ("❌ Runtime error:", "❌ Unexpected error:",
                  "isn't a valid command (unknown subcommand",
                  "is missing arguments (got")
_ALLOW_ERRORS_TAG = "harness-allows-errors"

# A failed `!assert` is a scenario checking its own end state and finding it
# wrong — always a failure, whatever HARNESS-ALLOWS-ERRORS says. Only a
# scenario demonstrating a failing assert on purpose opts out, with its own
# `HARNESS-ALLOWS-ASSERT-FAIL` tag.
_ASSERT_MARKERS = ("❌ Assertion failed", "❌ Expectation failed")
_ALLOW_ASSERT_TAG = "harness-allows-assert-fail"


class _Ctx:
    """Minimal ReplyContext stand-in: collects sent messages. Carries a
    switchable identity (default owner "cli") so scenarios can exercise
    the host/player gating with `!as host` / `!as player <name>`."""
    channel_key = "CLI"
    cli_mutable = True
    # `!as server <key>` moves the harness into another server's workspace
    # (run_one routes each command by guild_key), so scenarios can show two
    # servers' systems, matches and saves staying apart.
    workspace_switchable = True

    def __init__(self) -> None:
        self.out: List[str] = []
        self.user_id = "cli"
        self.user_name = "cli"
        self.guild_key = LOCAL_WORKSPACE

    @property
    def is_admin(self) -> bool:
        # Server-wide commands (!system edits, !store, !run, ...) need an
        # admin. The harness's owner identity stands in for a server
        # administrator; `!as player <name>` drops it, so scenarios can test
        # the refusal.
        return self.user_id == "cli"

    @property
    def is_bot_owner(self) -> bool:
        # Same stand-in for the bot owner (the `!owner` commands).
        return self.user_id == "cli"

    async def send(self, message: str) -> None:
        self.out.append(message)


def _interpret_escapes(raw: str) -> str:
    """Convert the documentation escapes `\\n` and `\\t` into the real
    characters the command layer would receive. Done on the raw line
    before shlex so a quoted multi-line formula keeps its newlines
    (shlex preserves real newlines inside quotes)."""
    return raw.replace("\\n", "\n").replace("\\t", "\t")


def parse_scenarios(path: str) -> List[Tuple[int, str, List[str], "frozenset[str]"]]:
    """Return [(number, title, [command_line, ...], allows), ...] in file
    order. `allows` holds "errors" when the scenario's prose carries the
    HARNESS-ALLOWS-ERRORS opt-out tag (a deliberate error-handling test) and
    "assert" for HARNESS-ALLOWS-ASSERT-FAIL (a deliberately failing !assert)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    out: List[Tuple[int, str, List[str], "frozenset[str]"]] = []
    for m in SCENARIO_RE.finditer(text):
        num = int(m.group(1))
        title = m.group(2).strip()
        body = m.group(3)
        low = body.lower()
        allows = frozenset(
            (["errors"] if _ALLOW_ERRORS_TAG in low else [])
            + (["assert"] if _ALLOW_ASSERT_TAG in low else []))
        # Commands live ABOVE the "Expected:" prose. Stop collecting at
        # the Expected marker so prose lines that happen to start with
        # `!` (e.g. "!ent info shows ...", "!map renders ...") aren't
        # mistaken for commands.
        cmds: List[str] = []
        for ln in body.splitlines():
            if ln.strip().lower().startswith("expected:"):
                break
            if ln.startswith("!") or ln.startswith(_EXPECT_PREFIXES):
                cmds.append(ln)
        out.append((num, title, cmds, allows))
    return out


_EXPECT_PREFIXES = ("??", "?!")


def _check_expectation(line: str, previous: List[str]) -> str:
    """The transcript reply for a `?? text` / `?! text` line, checked
    against the previous command's reply."""
    want = line[2:].strip()
    if len(want) >= 2 and want[0] == want[-1] == '"':
        want = want[1:-1]
    reply = "\n".join(previous)
    if not want:
        return f"❌ Expectation failed: `{line}` has no text to look for."
    present = want in reply
    if line.startswith("??"):
        return ("✅ reply contains the text" if present else
                f"❌ Expectation failed: the reply doesn't contain {want!r}")
    return ("✅ reply doesn't contain the text" if not present else
            f"❌ Expectation failed: the reply contains {want!r}")


async def run_one(cmds: List[str]) -> List[Tuple[str, List[str]]]:
    """Run a scenario's commands against fresh workspaces (the local one
    unless `!as server <key>` switches). Returns
    [(command_line, [output_line, ...]), ...]. A `??` / `?!` line checks the
    reply of the command before it (see the module docstring)."""
    workspaces = Workspaces()
    ctx = _Ctx()
    transcript: List[Tuple[str, List[str]]] = []
    previous: List[str] = []
    for line in cmds:
        if line.startswith(_EXPECT_PREFIXES):
            transcript.append((line, [_check_expectation(line, previous)]))
            continue
        ctx.out = []
        body = _interpret_escapes(line.lstrip("!"))
        try:
            parts = shlex.split(body)
        except ValueError as e:
            transcript.append((line, [f"💥 shlex parse error: {e}"]))
            continue
        if not parts:
            continue
        try:
            mgr = workspaces.get(ctx.guild_key)
            await registry.run(parts[0], parts[1:], ctx, mgr)
        except Exception as e:  # noqa: BLE001 - surface as a flagged failure
            ctx.out.append(f"💥 Uncaught: {type(e).__name__}: {e}")
        transcript.append((line, list(ctx.out)))
        previous = list(ctx.out)
    return transcript


def _flagged(transcript: List[Tuple[str, List[str]]],
             allows: "frozenset[str]" = frozenset()) -> List[Tuple[str, str]]:
    """Return [(command, output_line), ...] for every flagged failure. 💥 /
    Syntax error always flag; a top-level ❌ Runtime/Unexpected error flags too
    unless the scenario opted out ("errors" in allows); a failed !assert
    flags unless "assert" is in allows."""
    markers = _FAILURE_MARKERS
    if "errors" not in allows:
        markers = markers + _ERROR_MARKERS
    if "assert" not in allows:
        markers = markers + _ASSERT_MARKERS
    hits = []
    for cmd, outs in transcript:
        for o in outs:
            if any(marker in o for marker in markers):
                hits.append((cmd, o))
    return hits


_HEADER_RE = re.compile(r"^SCENARIO (\d+)\b", re.MULTILINE)


def header_problems(path: str,
                    scenarios: List[Tuple[int, str, List[str], bool]]) -> List[str]:
    """Cross-check every `SCENARIO N` header line against what SCENARIO_RE
    actually parsed. A header the regex misses (e.g. no blank line before it,
    or no dashes underline) is silently folded into the PREVIOUS scenario's
    Expected: prose — where command collection has already stopped — so its
    commands never run. That hid scenarios 230/232/236 for a long stretch.
    Also reports duplicate numbers. Returns human-readable problem lines."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    headers = [int(n) for n in _HEADER_RE.findall(text)]
    parsed = [s[0] for s in scenarios]
    problems = []
    unparsed = sorted(set(headers) - set(parsed))
    if unparsed:
        problems.append(
            f"header(s) not parsed (need a blank line before + a dashes "
            f"line after): {unparsed}")
    dupes = sorted({n for n in headers if headers.count(n) > 1})
    if dupes:
        problems.append(f"duplicate scenario number(s): {dupes}")
    return problems


def scenario_prose(path: str) -> Dict[int, str]:
    """{number: the scenario's prose from `Expected:` on} — for --review."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    out: Dict[int, str] = {}
    for m in SCENARIO_RE.finditer(text):
        body = m.group(3)
        idx = body.lower().find("\nexpected:")
        if body.lower().startswith("expected:"):
            idx = 0
        out[int(m.group(1))] = body[idx:].strip() if idx >= 0 else ""
    return out


def _print_review(num: int, title: str, transcript, prose: str) -> None:
    """Every command with its FULL reply, then the scenario's prose, so the
    two can be read side by side (the prose is documentation nobody checks
    by machine; this is the quick way to spot it drifting)."""
    print(f"\n{'=' * 72}\nSCENARIO {num} — {title}\n{'=' * 72}")
    for cmd, outs in transcript:
        print(f"> {cmd}")
        for o in outs:
            for ln in o.splitlines() or [""]:
                print(f"    {ln}")
    print(f"{'-' * 72}\n{prose}")


async def main_async(args: argparse.Namespace) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    seq_path = os.path.join(here, "test_sequences.txt")
    scenarios = parse_scenarios(seq_path)
    problems = header_problems(seq_path, scenarios)
    if problems:
        for p in problems:
            print(f"❌ test_sequences.txt: {p}")
        return 1

    if args.list:
        for num, title, _, _ in scenarios:
            print(f"{num:>4}  {title}")
        return 0

    wanted = set(args.scenarios)
    if wanted:
        scenarios = [s for s in scenarios if s[0] in wanted]
        missing = wanted - {s[0] for s in scenarios}
        if missing:
            print(f"⚠️ no such scenario(s): {sorted(missing)}")

    prose = scenario_prose(seq_path) if args.review else {}
    total_fail = 0
    for num, title, cmds, allows in scenarios:
        transcript = await run_one(cmds)
        hits = _flagged(transcript, allows)
        if args.review:
            _print_review(num, title, transcript, prose.get(num, ""))
        if args.verbose:
            print(f"\n=== SCENARIO {num} — {title} ===")
            for cmd, outs in transcript:
                joined = " / ".join(o.replace("\n", " ⏎ ") for o in outs)
                print(f"> {cmd[:60]:60} | {joined[:90]}")
        if hits:
            total_fail += 1
            print(f"\n❌ SCENARIO {num} — {title}")
            for cmd, o in hits:
                print(f"    {cmd[:70]}")
                print(f"      → {o.splitlines()[0][:100]}")

    print(
        f"\n{len(scenarios)} scenario(s) run; "
        f"{total_fail} with flagged failures."
    )
    return 1 if total_fail else 0


def main() -> None:
    # File commands (!store / !run / !history export|import) are confined to
    # vtt_commands.SAVES_DIR. Point it at a throwaway folder for the run, so
    # scenarios never touch — or leave junk in — a real `saves/` folder that
    # holds someone's campaign files.
    import shutil
    import tempfile
    import vtt_commands
    scratch = tempfile.mkdtemp(prefix="fcs_scenarios_")
    vtt_commands.SAVES_DIR = scratch
    try:
        _main()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _main() -> None:
    ap = argparse.ArgumentParser(description="Run test_sequences.txt scenarios.")
    ap.add_argument("scenarios", nargs="*", type=int,
                    help="scenario numbers to run (default: all)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print a per-command transcript")
    ap.add_argument("--list", action="store_true",
                    help="list scenario numbers and titles, then exit")
    ap.add_argument("--review", action="store_true",
                    help="print every command's full reply followed by the "
                         "scenario's Expected prose, to compare the two")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
