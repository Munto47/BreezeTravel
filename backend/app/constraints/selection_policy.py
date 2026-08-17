"""Evidence-aware candidate selection policy."""

from __future__ import annotations

from app.schemas.place import EvidenceStatus, Place


_EVIDENCE_RANK = {
    EvidenceStatus.VERIFIED: 0,
    EvidenceStatus.REQUIRES_CONFIRMATION: 1,
    EvidenceStatus.UNKNOWN: 2,
    None: 2,
}


def select_evidence_eligible_candidates(places: list[Place]) -> list[Place]:
    """Reject proven geo violations and keep unverified claims secondary."""

    eligible = [
        place for place in places
        if not any(
            item.status == EvidenceStatus.VERIFIED and item.satisfies_constraint is False
            for item in place.geo_evidence
        )
    ]
    # Python's sort is stable: within one evidence tier retain the semantic and
    # slot order established by CandidateSelection. Rating is a ranking input,
    # not an evidence concern, and must not overwrite an explicit cuisine match.
    return sorted(
        eligible,
        key=lambda place: _EVIDENCE_RANK.get(place.selection_evidence_status, 2),
    )
