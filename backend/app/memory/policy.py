"""Shared policy for identities that must not touch long-term memory."""

from __future__ import annotations


NON_PERSISTENT_USER_IDS = frozenset({"", "anonymous", "tool-call", "eval"})


def should_use_long_term_memory(user_id: str | None) -> bool:
    """Return whether the identity is eligible for preference load/save."""
    return bool(user_id and user_id not in NON_PERSISTENT_USER_IDS)
