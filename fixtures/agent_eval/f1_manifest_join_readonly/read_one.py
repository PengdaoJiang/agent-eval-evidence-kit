"""Print exactly one UTF-8 fixture file without permitting path traversal."""

from __future__ import annotations

import pathlib
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: read_one.py RELATIVE_PATH", file=sys.stderr)
        return 2
    root = pathlib.Path.cwd().resolve()
    candidate = (root / sys.argv[1]).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        print("path must remain inside fixture", file=sys.stderr)
        return 2
    if not candidate.is_file():
        print("fixture file not found", file=sys.stderr)
        return 1
    sys.stdout.write(candidate.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
