
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

def parse(line: str) -> List[str]:
    # supports quotes and spaces
    return shlex.split(line)

async def main():
    mgr = MatchManager()
    ctx = CLICtx()
    print("VTT CLI. Type commands like: !match new Test 10 8 | !match use <id> | !ent add Rogue 12 0 0 17 | !turn next | !state")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye"); break
        if not line: continue
        if line in {"quit","exit"}: break
        if line.startswith("!"):
            parts = parse(line[1:])
            root, *args = parts
            await registry.run(root, args, ctx, mgr)
        else:
            print("Commands must start with '!'")

if __name__ == "__main__":
    asyncio.run(main())