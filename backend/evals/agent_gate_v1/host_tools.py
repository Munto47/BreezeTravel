from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


class TrustedHostToolError(ValueError):
    pass


_WINDOWS_CANDIDATES = {
    "git": (
        Path(r"C:\Program Files\Git\cmd\git.exe"),
        Path(r"C:\Program Files\Git\bin\git.exe"),
    ),
    "docker": (
        Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"),
    ),
}

_POSIX_CANDIDATES = {
    "git": (
        Path("/usr/bin/git"),
        Path("/usr/local/bin/git"),
        Path("/opt/homebrew/bin/git"),
    ),
    "docker": (
        Path("/usr/bin/docker"),
        Path("/usr/local/bin/docker"),
        Path("/opt/homebrew/bin/docker"),
    ),
}


@lru_cache(maxsize=4)
def trusted_host_tool(name: str) -> str:
    """Resolve authority tools without consulting cwd, PATH, or candidate bytes."""

    candidates = _WINDOWS_CANDIDATES if os.name == "nt" else _POSIX_CANDIDATES
    if name not in candidates:
        raise TrustedHostToolError(f"unsupported authority host tool: {name}")
    for candidate in candidates[name]:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and resolved.is_absolute():
            return str(resolved)
    raise TrustedHostToolError(
        f"formal Agent Gate requires {name} in an authority-owned system location"
    )
