
## cli.py (desktop runner using the same commands)
# cli.py
import asyncio, shlex
from typing import List
from logic import MatchManager
from vtt_commands import registry

class CLICtx:
    channel_key = "CLI"
    async def send(self, message: str):
        print(message)

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