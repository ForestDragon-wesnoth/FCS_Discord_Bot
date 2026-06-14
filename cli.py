
## cli.py (desktop runner using the same commands)
# cli.py
import asyncio, shlex, os, sys
from typing import List
from logic import MatchManager
from vtt_commands import registry


def _enable_terminal_color() -> bool:
    """Best-effort: make this terminal able to render the colorized map.

    Returns True when ANSI escapes will be interpreted, False otherwise
    (the caller then disables color + warns instead of spewing raw codes).
    On Windows the legacy console doesn't process ANSI until
    ENABLE_VIRTUAL_TERMINAL_PROCESSING is turned on; we flip it via
    SetConsoleMode. NO_COLOR (the de-facto standard) and a non-tty stdout
    (piped/redirected) both force plain."""
    if os.environ.get("NO_COLOR"):
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if not k.GetConsoleMode(h, ctypes.byref(mode)):
                return False
            ENABLE_VT = 0x0004
            if not k.SetConsoleMode(h, mode.value | ENABLE_VT):
                return False
        except Exception:
            return False
    return True


class CLICtx:
    channel_key = "CLI"
    # Whether the colorized renderer is used. Set per-run in main() from
    # _enable_terminal_color(); a terminal that can't process ANSI gets
    # plain output (and a one-time warning) rather than raw escape codes.
    supports_color = True
    # The CLI is single-user and local, so identity is a switchable
    # stand-in (the `!as` command flips it) used to PREVIEW what a host
    # vs a player sees. Default identity "cli" owns any match it creates.
    cli_mutable = True
    # No host-approval infrastructure exists at the CLI (there's no second
    # person to approve a queued command), so the access gate is a no-op
    # here: every command runs directly regardless of the current `!as`
    # identity. `!as player` still changes the identity for previewing,
    # it just no longer bounces mutating commands to an approval dead-end.
    auto_approve = True

    def __init__(self):
        self.user_id = "cli"
        self.user_name = "cli"

    async def send(self, message: str):
        print(message)

    async def prompt_choice(self, prompt, options, lo, hi):
        """Interactive mid-action choice prompt (choose / choose_number).
        Returns the typed answer as a string, or None to cancel. Blocking
        input() is fine here: the CLI's main loop is already awaiting this
        command, and the engine is single-threaded."""
        if options is not None:
            shown = ", ".join(f"{i + 1}) {o}" for i, o in enumerate(options))
            print(f"? {prompt}\n  {shown}\n  (type a value, or 'cancel')")
        else:
            print(f"? {prompt} (enter a number {lo}-{hi}, or 'cancel')")
        try:
            line = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not line:
            return None
        # Allow picking an option by its 1-based number, too.
        if options is not None and line.isdigit():
            idx = int(line) - 1
            if 0 <= idx < len(options):
                return options[idx]
        return line

def parse(line: str):
    try:
        return shlex.split(line)
    except ValueError as e:
        # Catch unclosed quotes or other shlex errors
        raise RuntimeError(f"Parse error: {e}")

async def main():
    mgr = MatchManager()
    ctx = CLICtx()
    color_ok = _enable_terminal_color()
    CLICtx.supports_color = color_ok
    print(
        "VTT CLI. Type !help to see available commands\n"
        "Type !help [command] to see available subcommands for a specific command\n"
        "Type 'exit' or 'quit' to leave."
    )
    if not color_ok:
        print(
            "(note: this terminal can't render ANSI color — the map will "
            "show plain. Use Discord for colored units, or tell units apart "
            "with custom glyphs: `!ent set_var <id> glyph <char>`.)"
        )
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break

        if not line:
            continue
        if line in {"quit", "exit"}:
            break

        if line.startswith("!"):
            try:
                parts = parse(line[1:])
            except RuntimeError as e:
                print(f"❌ {e}")
                continue
            if not parts:
                continue

            root, *args = parts
            try:
                await registry.run(root, args, ctx, mgr)
            except Exception as e:
                # Surface command/logic errors without killing the CLI
                print(f"❌ {e}")
        else:
            print("Commands must start with '!'")

if __name__ == "__main__":
    asyncio.run(main())