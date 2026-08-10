"""Publish a public evaluation summary only after deterministic quality gates pass.

The publisher never recomputes scores; it validates the immutable artifacts
produced by the corpus/RAG/router jobs and atomically advances ``latest.json``.
That separation prevents README metrics from being updated by a partial run.
"""

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


GATES = {
    "blind_questions_per_city": 20,
    "citation_completeness": 1.0,
    "key_fact_recall": 0.75,
    "unsupported_assertion_rate": 0.05,
    "router_both_f1": 0.85,
    "router_macro_f1": 0.85,
}
REQUIRED = {"corpus", "rag", "router", "public_e2e", "metadata"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def validate_bundle(bundle: dict) -> list[str]:
    missing = REQUIRED - bundle.keys()
    errors = [f"missing section: {item}" for item in sorted(missing)]
    if errors:
        return errors
    corpus, rag, router, public_e2e = bundle["corpus"], bundle["rag"], bundle["router"], bundle["public_e2e"]
    if any(count < GATES["blind_questions_per_city"] for count in corpus.get("blind_questions_by_city", {}).values()):
        errors.append("every city needs at least 20 blind questions")
    if len(corpus.get("blind_questions_by_city", {})) < 3:
        errors.append("three-city blind split is required")
    for metric, minimum in (("citation_completeness", GATES["citation_completeness"]), ("key_fact_recall", GATES["key_fact_recall"])):
        if rag.get(metric, -1) < minimum:
            errors.append(f"rag {metric} below gate")
    if rag.get("unsupported_assertion_rate", 1) > GATES["unsupported_assertion_rate"]:
        errors.append("rag unsupported_assertion_rate above gate")
    for metric in ("router_both_f1", "router_macro_f1"):
        if router.get(metric, -1) < GATES[metric]:
            errors.append(f"router {metric} below gate")
    if not bundle["metadata"].get("corpus_manifest_sha256"):
        errors.append("missing corpus manifest hash")
    if public_e2e.get("status") != "passed" or not public_e2e.get("base_url"):
        errors.append("authenticated public E2E smoke has not passed")
    return errors


def publish(bundle_path: Path, evidence_root: Path) -> Path:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    errors = validate_bundle(bundle)
    if errors:
        raise ValueError("Evidence gates failed: " + "; ".join(errors))
    run_id = bundle.get("run_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = evidence_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"Evidence run already exists: {run_id}")
    run_dir.mkdir(parents=True)
    manifest = {
        "schema_version": "1.0", "status": "verified_public_blind_run", "run_id": run_id,
        "published_at": _now(), "git_sha": _git_sha(), "gates": GATES, "results": bundle,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(bundle_path, run_dir / "bundle.json")
    latest = evidence_root / "latest.json"
    pending = evidence_root / ".latest.pending.json"
    pending.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(latest)
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--evidence-root", type=Path, default=Path("evidence"))
    args = parser.parse_args()
    print(publish(args.bundle, args.evidence_root))


if __name__ == "__main__":
    main()
