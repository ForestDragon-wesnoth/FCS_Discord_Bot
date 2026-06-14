
## cli.py (desktop runner using the same commands)
# cli.py
import asyncio, shlex
from typing import List
from logic import MatchManager
from vtt_commands import registry

class CLICtx:
    channel_key = "CLI"
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
    print(
        "VTT CLI. Type !help to see available commands\n"
        "Type !help [command] to see available subcommands for a specific command\n"
        "Type 'exit' or 'quit' to leave."
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