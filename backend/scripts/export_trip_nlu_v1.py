from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.trip_intake.models import LocationRole, TripIntakeExtraction


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _quotes(items: list[Any]) -> list[str]:
    return list(dict.fromkeys(span.quote for item in items for span in item.evidence))


def export_case(case: dict[str, Any]) -> dict[str, Any]:
    value = TripIntakeExtraction.model_validate(case["expected"])
    primary = next(
        (
            item
            for item in value.locations.mentions
            if item.mention_id == value.locations.primary_mention_id
        ),
        None,
    )
    by_role = {
        role: [
            item
            for item in value.locations.mentions
            if item.role == role
        ]
        for role in LocationRole
    }
    likes = [item for item in value.preferences.items if item.polarity.value == "LIKE"]
    dislikes = [item for item in value.preferences.items if item.polarity.value == "DISLIKE"]
    requirements = [item for item in value.preferences.items if item.polarity.value == "REQUIREMENT"]
    location_items = value.locations.mentions
    duration_items = [value.temporal.days, value.temporal.nights]
    preference_items: list[Any] = [*value.preferences.items, value.preferences.pace]
    return {
        "case_id": case["case_id"],
        "input_text": case["input_text"],
        "expected": {
            "locations": {
                "primary_city": primary.normalized_name.rstrip("市") if primary and primary.normalized_name else None,
                "destination_cities": [
                    (item.normalized_name or item.raw_text).rstrip("市")
                    for item in [
                        *by_role[LocationRole.PRIMARY_DESTINATION],
                        *by_role[LocationRole.DESTINATION_CANDIDATE],
                    ]
                ],
                "requested_places": [item.raw_text for item in by_role[LocationRole.REQUESTED_PLACE]],
                "origin_locations": [item.raw_text for item in by_role[LocationRole.ORIGIN]],
                "excluded_locations": [item.raw_text for item in by_role[LocationRole.EXCLUDED]],
                "other_location_mentions": [item.raw_text for item in by_role[LocationRole.OTHER_MENTION]],
                "status": value.locations.status.value.casefold(),
            },
            "party_size": {
                "min": value.party_size.total.min,
                "max": value.party_size.total.max,
                "type": value.party_size.total.quantifier.value.casefold(),
                "source": value.party_size.total.derivation.value.casefold(),
                "composition": value.party_size.composition.model_dump(mode="json"),
            },
            "duration": {
                "days": {
                    "min": value.temporal.days.min,
                    "max": value.temporal.days.max,
                    "type": value.temporal.days.quantifier.value.casefold(),
                    "source": value.temporal.days.derivation.value.casefold(),
                },
                "nights": {
                    "min": value.temporal.nights.min,
                    "max": value.temporal.nights.max,
                    "type": value.temporal.nights.quantifier.value.casefold(),
                    "source": value.temporal.nights.derivation.value.casefold(),
                },
            },
            "preferences": {
                "likes": [item.label for item in likes],
                "dislikes": [item.label for item in dislikes],
                "pace": value.preferences.pace.value.value.casefold(),
                "requirements": [item.label for item in requirements],
            },
        },
        "annotation": {
            "difficulty": case["annotation"]["difficulty"],
            "noise_types": case["annotation"]["noise_types"],
            "coverage_tags": [],
            "evidence_spans": {
                "locations": _quotes(location_items),
                "party_size": _quotes([value.party_size.total]),
                "duration": _quotes(duration_items),
                "preferences": _quotes(preference_items),
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        raise ValueError("v1 compatibility export must not overwrite v2 oracle")
    values = [export_case(case) for case in _read_jsonl(args.source)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
