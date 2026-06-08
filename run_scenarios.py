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

Usage:
    python run_scenarios.py                 # run all, summarize failures
    python run_scenarios.py -v              # also print a transcript
    python run_scenarios.py 244 245         # run only these scenarios
    python run_scenarios.py --list          # list scenario numbers/titles

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

from logic import MatchManager
from vtt_commands import registry

SCENARIO_RE = re.compile(
    r"\nSCENARIO (\d+)\s*[—-]\s*([^\n]*)\n[-]+\n(.*?)(?=\n\nSCENARIO |\Z)",
    re.DOTALL,
)

# Output substrings that mark a flagged failure. 💥 is the dispatcher's
# unexpected-exception prefix; "Syntax error" is a formula parse failure
# (see module docstring for why that's always a malformed scenario).
_FAILURE_MARKERS = ("💥", "Syntax error")


class _Ctx:
    """Minimal ReplyContext stand-in: collects sent messages. Carries a
    switchable identity (default owner "cli") so scenarios can exercise
    the host/player gating with `!as host` / `!as player <name>`."""
    channel_key = "CLI"
    cli_mutable = True

    def __init__(self) -> None:
        self.out: List[str] = []
        self.user_id = "cli"
        self.user_name = "cli"

    async def send(self, message: str) -> None:
        self.out.append(message)


def _interpret_escapes(raw: str) -> str:
    """Convert the documentation escapes `\\n` and `\\t` into the real
    characters the command layer would receive. Done on the raw line
    before shlex so a quoted multi-line formula keeps its newlines
    (shlex preserves real newlines inside quotes)."""
    return raw.replace("\\n", "\n").replace("\\t", "\t")


def parse_scenarios(path: str) -> List[Tuple[int, str, List[str]]]:
    """Return [(number, title, [command_line, ...]), ...] in file order."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    out: List[Tuple[int, str, List[str]]] = []
    for m in SCENARIO_RE.finditer(text):
        num = int(m.group(1))
        title = m.group(2).strip()
        body = m.group(3)
        # Commands live ABOVE the "Expected:" prose. Stop collecting at
        # the Expected marker so prose lines that happen to start with
        # `!` (e.g. "!ent info shows ...", "!map renders ...") aren't
        # mistaken for commands.
        cmds: List[str] = []
        for ln in body.splitlines():
            if ln.strip().lower().startswith("expected:"):
                break
            if ln.startswith("!"):
                cmds.append(ln)
        out.append((num, title, cmds))
    return out


async def run_one(cmds: List[str]) -> List[Tuple[str, List[str]]]:
    """Run a scenario's commands against a fresh MatchManager. Returns
    [(command_line, [output_line, ...]), ...]."""
    mgr = MatchManager()
    ctx = _Ctx()
    transcript: List[Tuple[str, List[str]]] = []
    for line in cmds:
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
            await registry.run(parts[0], parts[1:], ctx, mgr)
        except Exception as e:  # noqa: BLE001 - surface as a flagged failure
            ctx.out.append(f"💥 Uncaught: {type(e).__name__}: {e}")
        transcript.append((line, list(ctx.out)))
    return transcript


def _flagged(transcript: List[Tuple[str, List[str]]]) -> List[Tuple[str, str]]:
    """Return [(command, output_line), ...] for every flagged failure."""
    hits = []
    for cmd, outs in transcript:
        for o in outs:
            if any(marker in o for marker in _FAILURE_MARKERS):
                hits.append((cmd, o))
    return hits


async def main_async(args: argparse.Namespace) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    scenarios = parse_scenarios(os.path.join(here, "test_sequences.txt"))

    if args.list:
        for num, title, _ in scenarios:
            print(f"{num:>4}  {title}")
        return 0

    wanted = set(args.scenarios)
    if wanted:
        scenarios = [s for s in scenarios if s[0] in wanted]
        missing = wanted - {s[0] for s in scenarios}
        if missing:
            print(f"⚠️ no such scenario(s): {sorted(missing)}")

    total_fail = 0
    for num, title, cmds in scenarios:
        transcript = await run_one(cmds)
        hits = _flagged(transcript)
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
    # Clean up any save artifacts scenarios may have dropped in cwd.
    for p in ("tpl_save", "tpl_save.json", "groups_test.json",
              "savetest.json", "test_compat"):
        if os.path.exists(p):
            os.remove(p)
    return 1 if total_fail else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Run test_sequences.txt scenarios.")
    ap.add_argument("scenarios", nargs="*", type=int,
                    help="scenario numbers to run (default: all)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print a per-command transcript")
    ap.add_argument("--list", action="store_true",
                    help="list scenario numbers and titles, then exit")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
