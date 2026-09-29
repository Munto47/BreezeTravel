"""Opaque source anchors for preserving decisions about individual visits."""
from __future__ import annotations

import hashlib
from uuid import NAMESPACE_URL, uuid5

from app.trip_understanding.models import ProposedMention


def source_occurrence_id(source: str | None, mention: ProposedMention) -> str | None:
    if not source or source[mention.span_start:mention.span_end] != mention.raw_text:
        return None
    name = mention.atomic_place_name
    # A broad quote containing two visits is ambiguous; never bind both to
    # the same decision. Narrow to a unique literal name when possible so
    # changing the surrounding quote does not change visit identity.
    if not name or mention.raw_text.count(name) != 1:
        return None
    start = mention.span_start + mention.raw_text.index(name)
    end = start + len(name)
    source_identity = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return uuid5(NAMESPACE_URL, f"source-visit:{source_identity}:{start}:{end}").hex
