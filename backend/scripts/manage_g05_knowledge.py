from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.db.connection import close_pool, get_pool
from app.trip_understanding.knowledge_admin import PostgresKnowledgeAdmin
from evals.g05_knowledge import evaluate_admission_manifest, load_admission_manifest


DEFAULT_MANIFEST = Path(__file__).parents[1] / "eval_data/g05_knowledge/admission_v1.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage G05 sourced knowledge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    import_parser = subparsers.add_parser("import", help="validate and import a source bundle")
    import_parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)

    for name, key_name in (("withdraw-source", "source_key"), ("withdraw-claim", "claim_key")):
        revoke = subparsers.add_parser(name)
        revoke.add_argument(key_name)
        revoke.add_argument("version", type=int)
        revoke.add_argument("--reason", required=True)
        revoke.add_argument("--reviewer", default="WP-G05-INTEGRATOR")
    subparsers.add_parser("readback")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    pool = await get_pool()
    admin = PostgresKnowledgeAdmin(pool)
    if args.command == "import":
        manifest = load_admission_manifest(args.manifest)
        report = evaluate_admission_manifest(manifest, as_of=now)
        if not report.passed:
            raise RuntimeError("knowledge admission failed: " + "; ".join(report.errors))
        outcome = await admin.import_manifest(manifest, imported_at=now)
        return {
            "status": "IMPORTED",
            "bundle_id": outcome.bundle_id,
            "bundle_hash": outcome.bundle_hash,
            "source_version_count": outcome.source_version_count,
            "claim_version_count": outcome.claim_version_count,
            "replayed": outcome.replayed,
        }
    if args.command == "withdraw-source":
        replayed = await admin.withdraw_source(
            source_key=args.source_key,
            version=args.version,
            reason=args.reason,
            reviewer=args.reviewer,
            withdrawn_at=now,
        )
        return {"status": "WITHDRAWN", "kind": "SOURCE", "replayed": replayed}
    if args.command == "withdraw-claim":
        replayed = await admin.withdraw_claim(
            claim_key=args.claim_key,
            version=args.version,
            reason=args.reason,
            reviewer=args.reviewer,
            withdrawn_at=now,
        )
        return {"status": "WITHDRAWN", "kind": "CLAIM", "replayed": replayed}
    return {"status": "READBACK", **await admin.readback()}


def main() -> int:
    args = _parser().parse_args()

    async def execute() -> dict[str, object]:
        try:
            return await _run(args)
        finally:
            await close_pool()

    result = asyncio.run(execute())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
