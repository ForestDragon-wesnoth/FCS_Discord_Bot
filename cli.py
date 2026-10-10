
## cli.py (desktop runner using the same commands)
# cli.py
import asyncio, os, sys
from typing import List
from logic import LOCAL_WORKSPACE
from storage import open_workspaces, load_local_settings, local_persistence_note
from vtt_commands import registry, split_command_line


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
    # The CLI fits wide output, so the map viewport stays OFF in 'auto'
    # mode (set the viewport_mode rule to 'on' to force it). See ReplyContext.
    viewport_capable = False
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
    # `!as server <key>` switches to another server's workspace (main()
    # routes each command by guild_key), for trying out server isolation.
    workspace_switchable = True

    def __init__(self):
        self.user_id = "cli"
        self.user_name = "cli"
        self.guild_key = LOCAL_WORKSPACE

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

def _install_readline(workspaces, ctx) -> bool:
    """Line editing for the prompt where the terminal has readline (Linux,
    macOS; pyreadline3 on Windows): this session's history on Up/Down, Ctrl+R
    reverse search, and Tab completion from completion.py (the GUI's). A
    second Tab lists the choices. Returns whether it is on."""
    try:
        import readline
    except ImportError:
        return False
    from completion import complete

    matches: List[str] = []

    def completer(text: str, state: int):
        if state == 0:
            matches.clear()
            try:
                line = readline.get_line_buffer()[:readline.get_endidx()]
                start, cands = complete(line, workspaces.get(ctx.guild_key),
                                        ctx)
                # readline replaces from ITS word start; ours can begin
                # later (after the `!`).
                lead = line[readline.get_begidx():start]
                if len(cands) == 1 and not cands[0].endswith((".", "=", ":")):
                    cands = [cands[0] + " "]
                matches.extend(lead + c for c in cands)
            except Exception:
                pass             # completion must never break the prompt
        return matches[state] if state < len(matches) else None

    readline.set_completer(completer)
    readline.set_completer_delims(" \t\n\"'")
    if "libedit" in (getattr(readline, "__doc__", "") or ""):
        readline.parse_and_bind("bind ^I rl_complete")     # macOS
    else:
        readline.parse_and_bind("tab: complete")
        # List the choices on the second Tab (GNU's default waits for a
        # third when the first one extended the word).
        readline.parse_and_bind("set show-all-if-unmodified on")
    return True


def parse(line: str):
    try:
        return split_command_line(line)
    except ValueError as e:
        # Catch unclosed quotes or other shlex errors
        raise RuntimeError(f"Parse error: {e}")

async def main():
    # The local workspace (and any `!as server` one) persists to data/ when
    # local_settings.json turns match persistence on.
    local = load_local_settings()
    workspaces = open_workspaces(persist=local["match_persistence"])
    ctx = CLICtx()
    from storage import reload_workspaces
    ctx.restart_workspaces = lambda: reload_workspaces(workspaces)
    color_ok = _enable_terminal_color()
    CLICtx.supports_color = color_ok
    print(
        "VTT CLI. Type !help to see available commands\n"
        "Type !help [command] to see available subcommands for a specific command\n"
        "Type 'exit' or 'quit' to leave."
    )
    note = local_persistence_note(local)
    if note:
        print(note)
    if _install_readline(workspaces, ctx):
        print("Tab completes (twice lists the choices) · Up/Down recall "
              "commands · Ctrl+R searches them.")
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
                await registry.run(root, args, ctx,
                                   workspaces.get(ctx.guild_key))
            except Exception as e:
                # Surface command/logic errors without killing the CLI
                print(f"❌ {e}")
        else:
            print("Commands must start with '!'")
    # Every command already wrote its changes; write once more on the way out.
    workspaces.storage.commit_all(workspaces)

if __name__ == "__main__":
    asyncio.run(main())