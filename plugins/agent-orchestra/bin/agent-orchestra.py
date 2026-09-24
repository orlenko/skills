#!/usr/bin/env python3
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if len(sys.argv) > 1 and sys.argv[1] in ("hook-context", "hook-stop", "hook-wait"):
    from agent_orchestra import fasthook

    if fasthook.nothing_to_do(sys.argv):
        sys.exit(0)

from agent_orchestra.cli import main


if __name__ == "__main__":
    main()
