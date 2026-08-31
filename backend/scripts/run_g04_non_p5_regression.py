from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
NON_P5_PYTEST_ARGS: Final[tuple[str, ...]] = (
    "-q",
    "tests",
    "--ignore-glob=tests/test_trip_check_p5*.py",
)


def main() -> int:
    """Run the historical non-P5 suite with pytest's native verdict."""

    os.chdir(BACKEND_ROOT)
    return int(pytest.main(list(NON_P5_PYTEST_ARGS)))


if __name__ == "__main__":
    raise SystemExit(main())
