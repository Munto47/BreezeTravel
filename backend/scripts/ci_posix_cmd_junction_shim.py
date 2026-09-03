#!/usr/bin/env python3
"""Provide the one ``cmd /c mklink /J`` operation used by legacy Linux CI.

The historical security test was authored for Windows. On POSIX, a directory
symlink exercises the same parent-path indirection that the scorer must reject.
No other cmd.exe command is accepted.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) != 5 or [item.casefold() for item in args[:3]] != [
        "/c",
        "mklink",
        "/j",
    ]:
        print("unsupported cmd compatibility invocation", file=sys.stderr)
        return 2

    link = Path(args[3])
    target = Path(args[4])
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            print(completed.stderr or completed.stdout, file=sys.stderr)
        return completed.returncode
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"junction-compatible directory link created: {link} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
