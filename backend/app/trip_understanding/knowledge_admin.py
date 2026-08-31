from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class KnowledgeAdminConflictError(RuntimeError):
    """An idempotency key or immutable version was reused with different content."""


class KnowledgeAdminNotFoundError(RuntimeError):
    """The requested source or claim version does not exist."""


@dataclass(frozen=True)
class KnowledgeImportOutcome:
    bundle_id: str
    bundle_hash: str
    source_version_count: int
    claim_version_count: int
    replayed: bool


def canonical_json_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    digest = canonical_json_hash([str(part) for part in parts])
    return f"{prefix}_{digest[:32]}"


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError("knowledge timestamps must be ISO-8601 strings or datetimes")


def compile_knowledge_bundle(manifest: dict[str, Any]) -> dict[str, Any]:
    bundle_id = str(manifest["dataset_id"])
    sources: list[dict[str, Any]] = []
    source_ids: dict[tuple[str, int], str] = {}
    admitted_sources = [
        raw
        for raw in manifest["sources"]
        if raw.get("admission_status") == "ADMITTED"
    ]
    for raw in sorted(
        admitted_sources,
        key=lambda value: (value["source_key"], int(value["version"])),
    ):
        source_key = str(raw["source_key"])
        version = int(raw["version"])
        source_version_id = _stable_id("ksv", source_key, version)
        source_ids[(source_key, version)] = source_version_id
        content = {
            key: raw[key]
            for key in (
                "source_key",
                "version",
                "publisher_name",
                "source_tier",
                "canonical_url",
                "access_method",
                "terms_url",
                "license_status",
                "storage_policy",
                "admission_status",
                "observed_at",
                "reviewed_at",
                "expires_at",
                "reviewer",
                "withdrawal_method",
                "version_note",
            )
        }
        sources.append(
            {
                **content,
                "source_version_id": source_version_id,
                "content_hash": canonical_json_hash(content),
            }
        )

    claims: list[dict[str, Any]] = []
    for raw in sorted(
        manifest["claims"],
        key=lambda value: (value["claim_key"], int(value["version"])),
    ):
        claim_key = str(raw["claim_key"])
        version = int(raw["version"])
        source_binding = (str(raw["source_key"]), int(raw["source_version"]))
        source_version_id = source_ids.get(source_binding)
        if source_version_id is None:
            raise ValueError(f"claim {claim_key} references an absent source version")
        conditions = raw["conditions"]
        content = {
            key: raw[key]
            for key in (
                "claim_key",
                "version",
                "canonical_place_id",
                "city",
                "claim_type",
                "suggestion_text",
                "short_evidence",
                "effective_at",
                "expires_at",
                "reviewer",
            )
        }
        content.update(
            {
                "source_version_id": source_version_id,
                "conditions": conditions,
            }
        )
        claims.append(
            {
                **content,
                "claim_revision_id": _stable_id("kcv", claim_key, version),
                "conditions_hash": canonical_json_hash(conditions),
                "content_hash": canonical_json_hash(content),
                "supersedes_claim_revision_id": (
                    _stable_id("kcv", claim_key, version - 1) if version > 1 else None
                ),
            }
        )

    return {
        "bundle_id": bundle_id,
        "bundle_hash": canonical_json_hash(manifest),
        "reviewer": str(manifest.get("reviewer") or "WP-G05-INTEGRATOR"),
        "sources": sources,
        "claims": claims,
    }


class PostgresKnowledgeAdmin:
    def __init__(self, pool: Any):
        self._pool = pool

    async def import_manifest(
        self,
        manifest: dict[str, Any],
        *,
        imported_at: datetime,
    ) -> KnowledgeImportOutcome:
        bundle = compile_knowledge_bundle(manifest)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                bundle["bundle_id"],
            )
            existing = await conn.fetchrow(
                """
                SELECT bundle_hash, source_version_count, claim_version_count
                FROM knowledge_import_receipts WHERE bundle_id = $1
                """,
                bundle["bundle_id"],
            )
            if existing is not None:
                if existing["bundle_hash"].strip() != bundle["bundle_hash"]:
                    raise KnowledgeAdminConflictError(
                        "bundle id was already imported with different content"
                    )
                return KnowledgeImportOutcome(
                    bundle_id=bundle["bundle_id"],
                    bundle_hash=bundle["bundle_hash"],
                    source_version_count=int(existing["source_version_count"]),
                    claim_version_count=int(existing["claim_version_count"]),
                    replayed=True,
                )

            for source in bundle["sources"]:
                await self._insert_source_version(conn, source)
            for claim in bundle["claims"]:
                await self._insert_claim_version(conn, claim)
            await conn.execute(
                """
                INSERT INTO knowledge_import_receipts (
                    import_receipt_id, bundle_id, bundle_hash, source_version_count,
                    claim_version_count, reviewer, imported_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                _stable_id("kir", bundle["bundle_id"], bundle["bundle_hash"]),
                bundle["bundle_id"],
                bundle["bundle_hash"],
                len(bundle["sources"]),
                len(bundle["claims"]),
                bundle["reviewer"],
                imported_at,
            )
        return KnowledgeImportOutcome(
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_hash"],
            source_version_count=len(bundle["sources"]),
            claim_version_count=len(bundle["claims"]),
            replayed=False,
        )

    async def _insert_source_version(self, conn: Any, source: dict[str, Any]) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"g05-knowledge-source:{source['source_key']}",
        )
        existing = await conn.fetchrow(
            """
            SELECT source_version_id, content_hash
            FROM knowledge_source_versions
            WHERE source_key = $1 AND version = $2
            """,
            source["source_key"],
            source["version"],
        )
        if existing is not None:
            if (
                existing["source_version_id"] != source["source_version_id"]
                or existing["content_hash"].strip() != source["content_hash"]
            ):
                raise KnowledgeAdminConflictError("source version content cannot be replaced")
            return
        latest = await conn.fetchval(
            "SELECT MAX(version) FROM knowledge_source_versions WHERE source_key = $1",
            source["source_key"],
        )
        expected = 1 if latest is None else int(latest) + 1
        if source["version"] != expected:
            raise KnowledgeAdminConflictError(
                f"source version must be appended sequentially; expected {expected}"
            )
        await conn.execute(
            """
            INSERT INTO knowledge_source_versions (
                source_version_id, source_key, version, publisher_name, source_tier,
                canonical_url, access_method, terms_url, license_status, storage_policy,
                admission_status, observed_at, reviewed_at, expires_at, reviewer,
                withdrawal_method, version_note, content_hash
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                $12::timestamptz, $13::timestamptz, $14::timestamptz,
                $15, $16, $17, $18
            )
            """,
            source["source_version_id"],
            source["source_key"],
            source["version"],
            source["publisher_name"],
            source["source_tier"],
            source["canonical_url"],
            source["access_method"],
            source["terms_url"],
            source["license_status"],
            source["storage_policy"],
            source["admission_status"],
            _as_datetime(source["observed_at"]),
            _as_datetime(source["reviewed_at"]),
            _as_datetime(source["expires_at"]),
            source["reviewer"],
            source["withdrawal_method"],
            source["version_note"],
            source["content_hash"],
        )

    async def _insert_claim_version(self, conn: Any, claim: dict[str, Any]) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"g05-knowledge-claim:{claim['claim_key']}",
        )
        existing = await conn.fetchrow(
            """
            SELECT claim_revision_id, content_hash
            FROM knowledge_claim_versions
            WHERE claim_key = $1 AND version = $2
            """,
            claim["claim_key"],
            claim["version"],
        )
        if existing is not None:
            if (
                existing["claim_revision_id"] != claim["claim_revision_id"]
                or existing["content_hash"].strip() != claim["content_hash"]
            ):
                raise KnowledgeAdminConflictError("claim version content cannot be replaced")
            return
        latest = await conn.fetchval(
            "SELECT MAX(version) FROM knowledge_claim_versions WHERE claim_key = $1",
            claim["claim_key"],
        )
        expected = 1 if latest is None else int(latest) + 1
        if claim["version"] != expected:
            raise KnowledgeAdminConflictError(
                f"claim version must be appended sequentially; expected {expected}"
            )
        if claim["supersedes_claim_revision_id"] is not None:
            predecessor = await conn.fetchval(
                "SELECT claim_revision_id FROM knowledge_claim_versions WHERE claim_revision_id = $1",
                claim["supersedes_claim_revision_id"],
            )
            if predecessor is None:
                raise KnowledgeAdminConflictError("claim predecessor is missing")
        await conn.execute(
            """
            INSERT INTO knowledge_claim_versions (
                claim_revision_id, claim_key, version, source_version_id,
                canonical_place_id, city, claim_type, conditions_json, conditions_hash,
                suggestion_text, short_evidence, effective_at, expires_at, reviewer,
                content_hash, supersedes_claim_revision_id
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11,
                $12::timestamptz, $13::timestamptz, $14, $15, $16
            )
            """,
            claim["claim_revision_id"],
            claim["claim_key"],
            claim["version"],
            claim["source_version_id"],
            claim["canonical_place_id"],
            claim["city"],
            claim["claim_type"],
            json.dumps(claim["conditions"], ensure_ascii=False, sort_keys=True),
            claim["conditions_hash"],
            claim["suggestion_text"],
            claim["short_evidence"],
            _as_datetime(claim["effective_at"]),
            _as_datetime(claim["expires_at"]),
            claim["reviewer"],
            claim["content_hash"],
            claim["supersedes_claim_revision_id"],
        )

    async def withdraw_source(
        self,
        *,
        source_key: str,
        version: int,
        reason: str,
        reviewer: str,
        withdrawn_at: datetime,
    ) -> bool:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"g05-knowledge-source:{source_key}",
            )
            row = await conn.fetchrow(
                """
                SELECT source_version_id FROM knowledge_source_versions
                WHERE source_key = $1 AND version = $2
                """,
                source_key,
                version,
            )
            if row is None:
                raise KnowledgeAdminNotFoundError("source version does not exist")
            existing = await conn.fetchrow(
                """
                SELECT reason, reviewer FROM knowledge_source_withdrawals
                WHERE source_version_id = $1
                """,
                row["source_version_id"],
            )
            if existing is not None:
                if existing["reason"] != reason or existing["reviewer"] != reviewer:
                    raise KnowledgeAdminConflictError("source withdrawal is already recorded")
                return True
            await conn.execute(
                """
                INSERT INTO knowledge_source_withdrawals (
                    withdrawal_id, source_version_id, reason, reviewer, withdrawn_at
                ) VALUES ($1, $2, $3, $4, $5)
                """,
                _stable_id("ksw", row["source_version_id"], reason),
                row["source_version_id"],
                reason,
                reviewer,
                withdrawn_at,
            )
        return False

    async def withdraw_claim(
        self,
        *,
        claim_key: str,
        version: int,
        reason: str,
        reviewer: str,
        withdrawn_at: datetime,
    ) -> bool:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"g05-knowledge-claim:{claim_key}",
            )
            row = await conn.fetchrow(
                """
                SELECT claim_revision_id FROM knowledge_claim_versions
                WHERE claim_key = $1 AND version = $2
                """,
                claim_key,
                version,
            )
            if row is None:
                raise KnowledgeAdminNotFoundError("claim version does not exist")
            existing = await conn.fetchrow(
                """
                SELECT reason, reviewer FROM knowledge_claim_withdrawals
                WHERE claim_revision_id = $1
                """,
                row["claim_revision_id"],
            )
            if existing is not None:
                if existing["reason"] != reason or existing["reviewer"] != reviewer:
                    raise KnowledgeAdminConflictError("claim withdrawal is already recorded")
                return True
            await conn.execute(
                """
                INSERT INTO knowledge_claim_withdrawals (
                    withdrawal_id, claim_revision_id, reason, reviewer, withdrawn_at
                ) VALUES ($1, $2, $3, $4, $5)
                """,
                _stable_id("kcw", row["claim_revision_id"], reason),
                row["claim_revision_id"],
                reason,
                reviewer,
                withdrawn_at,
            )
        return False

    async def readback(self) -> dict[str, int]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM knowledge_source_versions) AS source_versions,
                    (SELECT COUNT(*) FROM knowledge_claim_versions) AS claim_versions,
                    (SELECT COUNT(*) FROM knowledge_source_withdrawals) AS source_withdrawals,
                    (SELECT COUNT(*) FROM knowledge_claim_withdrawals) AS claim_withdrawals,
                    (SELECT COUNT(*) FROM knowledge_import_receipts) AS import_receipts,
                    (SELECT COUNT(*) FROM knowledge_usage_receipts) AS usage_receipts
                """
            )
        return {key: int(row[key]) for key in row.keys()}
