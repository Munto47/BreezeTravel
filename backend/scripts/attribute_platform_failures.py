"""Offline source/raw/projection comparison. Private evidence, never automatic gold edits."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from scripts.collect_platform_corpus import save_json
from scripts.measure_platform_corpus import read_corpus


def names_for(item):
    return {item["name"], *item.get("acceptable_names", [])}


def source_excerpts(source, names):
    excerpts = []
    for name in sorted(names, key=len, reverse=True):
        cursor = 0
        while (index := source.find(name, cursor)) >= 0:
            left = max(source.rfind(mark, 0, index) for mark in "\n。；;") + 1
            right = min((pos for mark in "\n。；;" if (pos := source.find(mark, index + len(name))) >= 0), default=len(source))
            excerpt = source[max(left, index - 180):min(right, index + len(name) + 260)]
            if excerpt not in excerpts:
                excerpts.append(excerpt)
            cursor = index + len(name)
    return excerpts


def attribute(manifest: Path, measurement: Path, labels: Path, raw_directory: Path) -> dict:
    report = json.loads(measurement.read_text(encoding="utf-8"))
    if {row["split"] for row in report["cases"]} != {"development"}:
        raise ValueError("Error attribution for iterative development may only read development cases")
    cases = {row["id"]: row for row in read_corpus(manifest, case_ids={row["case_id"] for row in report["cases"]})}
    golds = {row["case_id"]: row for row in json.loads(labels.read_text(encoding="utf-8"))["annotations"] if row["case_id"] in cases}
    results = []
    for row in report["cases"]:
        case_id = row["case_id"]
        source = cases[case_id]["text"]
        gold = golds[case_id]["activities"]
        actual = row.get("observations", [])
        raw = []
        for path in sorted(raw_directory.glob(f"{case_id}-r{row['repeat']}-call*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            for choice in record.get("choices", []):
                try:
                    draft = json.loads(choice.get("content") or "")
                except ValueError:
                    continue
                raw.extend({**item, "call": record["call"], "raw_file": path.name} for item in draft.get("activities", []))
        available = set(range(len(actual)))
        missing = []
        for item in gold:
            names = names_for(item)
            exact = next((i for i in sorted(available) if actual[i].get("name") in names
                and actual[i].get("role") == item["role"] and actual[i].get("day_index") == item["day_index"]), None)
            if exact is not None:
                available.remove(exact)
                if item.get("hierarchy_level") in {"subsite", "branch", "access"}:
                    missing.append({"gold": item, "category": "MATCHED", "source_excerpts": source_excerpts(source, names),
                        "observed": [actual[exact]], "raw": [v for v in raw if v.get("place_name") in names]})
                continue
            raw_same = [v for v in raw if v.get("place_name") in names]
            actual_same = [v for v in actual if v.get("name") in names]
            variants = [v for v in raw if v.get("place_name") and any(
                name in v["place_name"] or v["place_name"] in name for name in names) and v not in raw_same]
            correct_raw = [v for v in raw_same if v.get("role") == item["role"] and v.get("day_index") == item["day_index"]]
            if correct_raw:
                category = "POSTPROCESS_ROLE_OR_DAY" if actual_same else "VALIDATOR_OR_REPAIR_LOSS"
            elif raw_same:
                category = "MODEL_ROLE_OR_DAY"
            elif variants:
                category = "NAME_VARIANT_REVIEW"
            else:
                category = "MODEL_NOT_EXTRACTED"
            missing.append({"gold": item, "category": category, "source_excerpts": source_excerpts(source, names),
                "observed": actual_same, "raw": raw_same, "name_variant_candidates": variants})
        extras = []
        for index in sorted(available):
            item = actual[index]
            if item.get("role") not in {"PLANNED", "OPTIONAL"} or not item.get("name"):
                continue
            extras.append({"observed": item, "source_excerpts": source_excerpts(source, {item["name"]}),
                "gold_same_name": [v for v in gold if item["name"] in names_for(v)],
                "raw": [v for v in raw if v.get("place_name") == item["name"]]})
        results.append({"case_id": case_id, "missing_and_hierarchy": missing, "extra_or_misassigned": extras})
    return {"schema_version": "private-offline-attribution-v1", "external_calls": 0,
        "measurement": str(measurement.resolve()), "runtime_fingerprint": report["source_fingerprint"],
        "label_policy": "FIXED_INDEPENDENT_LABELS_NO_EDITS", "split": "development",
        "classification_limit": "Stage hypotheses, not semantic verdicts; variants and repeated occurrences require source review",
        "missing_by_stage": dict(Counter(item["category"] for case in results for item in case["missing_and_hierarchy"] if item["category"] != "MATCHED")),
        "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--measurement", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--raw-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Attribution cannot overwrite earlier evidence")
    result = attribute(args.manifest, args.measurement, args.labels, args.raw_directory)
    save_json(args.output, result)
    print(json.dumps(result["missing_by_stage"]))


if __name__ == "__main__":
    main()
