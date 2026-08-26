from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta


@dataclass(frozen=True)
class EvidencePolicy:
    version: str = "evidence-policy-v1"
    ttl_by_fact_type: dict[str, timedelta] = field(default_factory=lambda: {
        "POI_IDENTITY": timedelta(days=7),
        "OPENING_HOURS": timedelta(hours=72),
        "TEMPORARY_CLOSURE": timedelta(hours=24),
        "RESERVATION_POLICY": timedelta(hours=24),
        # Menu substitutions and cross-contamination handling can change with
        # suppliers and a venue's current menu; an old tag is not proof.
        "DIETARY_SUPPORT": timedelta(hours=24),
        "ROUTE_TIME": timedelta(minutes=15),
        "WEATHER": timedelta(hours=6),
        "PRICE_REFERENCE": timedelta(hours=24),
        "AGE_HEIGHT_POLICY": timedelta(days=7),
        "ACCESSIBILITY_POLICY": timedelta(days=7),
    })

    def ttl_for(self, fact_type: str) -> timedelta | None:
        return self.ttl_by_fact_type.get(fact_type)
