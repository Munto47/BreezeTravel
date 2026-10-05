"""Bounded follow-up jobs on the existing understanding resource and source."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import Field

from app.trip_understanding.bounded_supplement import SupplementPatch
from app.trip_understanding.errors import (
    IdempotencyConflictError, JobLeaseLostError, ResourceAccessDeniedError, ResourceGoneError,
    ResourceNotFoundError, ResourceNotReadyError, RevisionConflictError, SourceUnavailableError,
)
from app.trip_understanding.inference_allowance import InferenceAllowanceExceeded
from app.trip_understanding.models import PublicResourceRecord, SourceSemanticPlan, StrictModel, UserFacingTripResult
from app.trip_understanding.supplement_context import open_source_plan


class SupplementRequest(StrictModel):
    retry: bool = False


class SupplementStateView(StrictModel):
    status: Literal["AVAILABLE", "UNAVAILABLE", "QUEUED", "RUNNING", "APPLIED", "NO_CHANGES", "FAILED", "CANCELLED"]
    message: str
    job_id: str | None = None
    base_etag: str | None = None
    result_etag: str | None = None
    added_count: int = 0
    rejected: list[dict[str, str]] = Field(default_factory=list)
    available_actions: list[Literal["REQUEST", "RETRY", "CANCEL"]] = Field(default_factory=list)
    reason: str | None = None


class SupplementRejectedError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass
class SupplementWork:
    source: str
    plan: SourceSemanticPlan
    result: UserFacingTripResult
    resource: PublicResourceRecord
    base_etag: str


@dataclass
class SupplementCommit:
    job: object
    work: SupplementWork
    patch: SupplementPatch
    provider_binding: dict


@dataclass
class SupplementCommitCommand:
    # Deliberately absent from the public command union. Only the leased worker
    # may submit a validated patch via the ordinary atomic version writer.
    command_type: str = "SOURCE_SUPPLEMENT"


def _json(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def _request_hash(etag, retry):
    return hashlib.sha256(json.dumps({"etag": etag, "retry": retry}, sort_keys=True).encode()).hexdigest()


def supplement_mutation(commit, current):
    from app.trip_understanding.commands import (
        PublicCommandMutation, refresh_choice_selection_tokens, refresh_dining_access, refresh_meal_slot_tokens,
    )
    from app.trip_understanding.lodging_recovery import result_cards
    from app.trip_understanding.models import MapReadinessView

    result = commit.patch.result.model_copy(deep=True)
    existing = {card.activity_token for card in result_cards(current)}
    token_map = {}
    for card in result_cards(result):
        if card.activity_token in existing:
            token_map[card.activity_token] = secrets.token_urlsafe(24)
            card.activity_token = token_map[card.activity_token]
    for pending in result.pending_lodgings:
        token_map[pending.pending_token] = secrets.token_urlsafe(24)
        pending.pending_token = token_map[pending.pending_token]
    refresh_meal_slot_tokens(result.days, token_map)
    refresh_choice_selection_tokens(result.days, token_map)
    refresh_dining_access(current, result, token_map)
    result.map = MapReadinessView(status="PREPARING", message="补全已保存，正在更新相关路线", available_actions=[])
    return PublicCommandMutation(result=result, token_map=token_map, changed_days=commit.patch.changed_days,
        routes_changed=commit.patch.routes_changed)


def supplemented_plan(commit):
    mentions = [*commit.work.plan.mentions]
    for accepted in commit.patch.validation.accepted:
        mentions.extend(accepted.mentions)
    return commit.work.plan.model_copy(update={"mentions": mentions})


async def finish_supplement(conn, commit, *, etag, now):
    outcome = {"added_count": len(commit.patch.validation.accepted), "rejected": commit.patch.validation.rejected,
        "result_etag": etag, "usage": commit.provider_binding}
    await conn.execute("""UPDATE trip_understanding_jobs SET status='SUCCEEDED',supplement_outcome_json=$2::jsonb,
        lease_owner=NULL,lease_until=NULL,finished_at=$3,updated_at=$3 WHERE job_id=$1""",
        commit.job.job_id, json.dumps(outcome), now)


async def persist_supplement_items(conn, commit, mutation, *, revision, source_id, cipher, now):
    """Keep new source items and their encrypted quotes in the version transaction."""
    import base64
    from app.trip_understanding.pipeline import EvidenceCompiler
    from app.trip_understanding.source_occurrence import source_occurrence_id

    accepted = {m.mention_id: m for item in commit.patch.validation.accepted for m in item.mentions}
    output = commit.patch.resolved_additions
    resolved = [a for a in output.activities if a.compiled.mention.mention_id in accepted] if output else []
    compiled = [a.compiled for a in resolved]
    ids = {a.activity_id for a in compiled}
    claims = [c for c in output.claims if c.activity_id in ids] if output else []
    missing = [m for key, m in accepted.items() if key not in {a.mention.mention_id for a in compiled}]
    if missing:
        extra, extra_claims, _ = EvidenceCompiler().compile(commit.work.source,
            commit.work.plan.model_copy(update={"mentions": missing}))
        compiled.extend(extra)
        claims.extend(extra_claims)
    places = {a.compiled.activity_id: a for a in resolved}
    alternatives = {a.source_occurrence_id: a for day in mutation.result.days for a in day.alternatives}
    for item in compiled:
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM trip_understanding_activities WHERE activity_id=$1)", item.activity_id):
            continue  # The ordinary version writer already persisted this main card.
        mention = item.mention
        activity = places.get(item.activity_id)
        place = activity.place if activity else None
        alternative = alternatives.get(source_occurrence_id(commit.work.source, mention))
        token = alternative.activity_token if alternative and alternative.activity_token else item.public_activity_token
        resolution = activity.resolution_status.value if activity else "NOT_ELIGIBLE"
        receipt = (activity.resolver_receipt or (place.provider_binding if place else {})) if activity else {}
        await conn.execute("""INSERT INTO trip_understanding_activities (
            activity_id,understanding_id,revision,public_activity_token,day_index,sequence_index,role,
            mention_text,atomic_place_name,category_hint,time_hint,eligible_for_place_search,
            resolution_status,canonical_place_id,resolver_receipt_json,created_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NULL,$11,$12,$13,$14::jsonb,$15)""",
            item.activity_id, commit.job.understanding_id, revision, token, mention.day_index, mention.sequence_index,
            mention.role.value, mention.raw_text, mention.atomic_place_name, mention.category_hint,
            item.eligible_for_place_search, resolution, place.canonical_place_id if place else None,
            json.dumps(receipt), now)
    for claim in claims:
        envelope = cipher.encrypt(claim.quote, source_id=source_id, content_hash=commit.job.input_hash,
            purpose=f"claim:{claim.claim_id}")
        await conn.execute("""INSERT INTO trip_understanding_source_claims(
            claim_id,understanding_id,revision,source_id,activity_id,claim_type,span_start,span_end,quote,created_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""", claim.claim_id, commit.job.understanding_id, revision,
            source_id, claim.activity_id, claim.claim_type, claim.span_start, claim.span_end,
            "enc:v1:" + base64.urlsafe_b64encode(envelope).decode("ascii"), now)


async def lock_resource(conn, understanding_id, *, public_resource_id=None, now):
    aggregate = await conn.fetchrow("SELECT * FROM trip_understandings WHERE understanding_id=$1 FOR UPDATE", understanding_id)
    if aggregate is None:
        raise ResourceNotFoundError("trip does not exist")
    if aggregate["state"] == "DELETED":
        raise ResourceGoneError("trip was deleted")
    if public_resource_id is not None and aggregate["public_resource_id"] != public_resource_id:
        raise ResourceAccessDeniedError("trip ownership changed")
    session = None
    if aggregate["anonymous_session_id"] is not None:
        session = await conn.fetchrow("SELECT * FROM trip_understanding_anonymous_sessions WHERE session_id=$1 FOR SHARE",
            aggregate["anonymous_session_id"])
        if session is None or session["revoked_at"] is not None:
            raise ResourceAccessDeniedError("trip session revoked")
    checked = await conn.fetchval("SELECT GREATEST($1::timestamptz,clock_timestamp())", now)
    if aggregate["source_expires_at"] <= checked or session is not None and session["expires_at"] <= checked:
        raise ResourceGoneError("trip expired")
    return aggregate


async def lock_supplement(conn, job, *, now, require_base=True):
    """Caller takes the existing resource advisory lock before this order."""
    row = await conn.fetchrow("SELECT * FROM trip_understanding_jobs WHERE job_id=$1 FOR UPDATE", job.job_id)
    if row is None or row["job_type"] != "SUPPLEMENT" or row["understanding_id"] != job.understanding_id:
        raise JobLeaseLostError("supplement job binding differs")
    aggregate = await lock_resource(conn, job.understanding_id, public_resource_id=row["base_public_resource_id"], now=now)
    source = await conn.fetchrow("""SELECT s.* FROM trip_understanding_sources s
        JOIN trip_understanding_revisions r ON r.source_id=s.source_id
        WHERE r.understanding_id=$1 AND r.revision=$2 FOR UPDATE OF s""", job.understanding_id, job.revision)
    checked = await conn.fetchval("SELECT GREATEST($1::timestamptz,clock_timestamp())", now)
    if (row["status"] != "RUNNING" or row["lease_owner"] != job.lease_owner or row["attempt"] != job.attempt
            or row["lease_until"] <= checked or row["revision"] != job.revision or row["input_hash"].strip() != job.input_hash):
        raise JobLeaseLostError("supplement claim expired or changed")
    if (aggregate["owner_user_id"] != row["base_owner_user_id"]
            or aggregate["anonymous_session_id"] != row["base_anonymous_session_id"]):
        raise ResourceAccessDeniedError("supplement owner changed")
    # Time may have elapsed after the session check while waiting for source.
    if aggregate["source_expires_at"] <= checked:
        raise ResourceGoneError("trip expired during supplement lock wait")
    if aggregate["anonymous_session_id"] is not None and not await conn.fetchval("""SELECT EXISTS(
        SELECT 1 FROM trip_understanding_anonymous_sessions WHERE session_id=$1 AND expires_at>$2 AND revoked_at IS NULL)""",
        aggregate["anonymous_session_id"], checked):
        raise ResourceGoneError("supplement session expired during lock wait")
    if (source is None or source["deleted_at"] is not None or source["encrypted_content"] is None
            or source["retention_until"] <= checked or source["content_hash"].strip() != job.input_hash):
        raise SourceUnavailableError("supplement source is no longer available")
    if source["inference_deadline_at"] is not None and source["inference_deadline_at"] <= checked:
        raise InferenceAllowanceExceeded("MODEL_CALL_DEADLINE_EXCEEDED")
    if require_base:
        etag = await conn.fetchval("SELECT opaque_etag FROM trip_understanding_results WHERE result_id=$1", aggregate["current_result_id"])
        if (aggregate["state"] not in {"READY", "PARTIAL"} or aggregate["current_revision"] != job.revision
                or etag != row["base_etag"]):
            raise RevisionConflictError("supplement base changed")
    return row, aggregate, source, checked


class PostgresSupplementRepositoryMixin:
    async def request_supplement(self, resource, *, expected_etag, idempotency_key, retry, now):
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("invalid idempotency key")
        key = hashlib.sha256(idempotency_key.encode()).hexdigest()
        request_hash = _request_hash(expected_etag, retry)
        scope = f"understanding:{resource.understanding_id}:supplement"
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"trip-understanding-resource:{resource.understanding_id}")
            aggregate = await lock_resource(conn, resource.understanding_id, public_resource_id=resource.public_resource_id, now=now)
            current = await conn.fetchrow("""SELECT p.opaque_etag,r.proposal_json,s.* FROM trip_understanding_results p
                JOIN trip_understanding_revisions r ON r.understanding_id=p.understanding_id AND r.revision=p.revision
                JOIN trip_understanding_sources s ON s.source_id=r.source_id WHERE p.result_id=$1 FOR UPDATE OF s""", aggregate["current_result_id"])
            checked = await conn.fetchval("SELECT GREATEST($1::timestamptz,clock_timestamp())", now)
            if current is None or aggregate["state"] not in {"READY", "PARTIAL"}:
                raise ResourceNotReadyError("trip cards are not ready")
            if current["deleted_at"] or not current["encrypted_content"] or current["retention_until"] <= checked:
                raise SourceUnavailableError("supplement source unavailable")
            if aggregate["source_expires_at"] <= checked:
                raise ResourceGoneError("trip expired")
            if aggregate["anonymous_session_id"] and not await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM trip_understanding_anonymous_sessions
                WHERE session_id=$1 AND expires_at>$2 AND revoked_at IS NULL)""", aggregate["anonymous_session_id"], checked):
                raise ResourceGoneError("trip session expired")
            previous = await conn.fetchrow("SELECT request_hash,response_json FROM trip_understanding_idempotency_records WHERE scope=$1 AND key_hash=$2", scope, key)
            if previous:
                if previous["request_hash"].strip() != request_hash:
                    raise IdempotencyConflictError("supplement request key was used for a different operation")
                response = SupplementStateView.model_validate(_json(previous["response_json"]))
                binding = await conn.fetchrow("SELECT * FROM trip_understanding_jobs WHERE job_id=$1", response.job_id)
                if (binding is None or binding["base_public_resource_id"] != aggregate["public_resource_id"]
                        or binding["base_owner_user_id"] != aggregate["owner_user_id"]
                        or binding["base_anonymous_session_id"] != aggregate["anonymous_session_id"]):
                    raise ResourceAccessDeniedError("supplement request ownership changed")
                return response
            if current["opaque_etag"] != expected_etag:
                raise RevisionConflictError("supplement request base differs")
            if "retained_source_plan" not in _json(current["proposal_json"]):
                raise SupplementRejectedError("SOURCE_CONTEXT_UNAVAILABLE")
            if current["inference_deadline_at"] is not None and current["inference_deadline_at"] <= checked:
                raise SupplementRejectedError("MODEL_CALL_DEADLINE_EXCEEDED")
            if current["inference_calls_remaining"] <= 0:
                raise SupplementRejectedError("MODEL_CALL_BUDGET_EXHAUSTED")
            count = current["supplement_requests"]
            if count >= 2 or retry != (count == 1):
                raise SupplementRejectedError("RETRY_REQUIRED" if count == 1 and not retry else "SUPPLEMENT_LIMIT")
            if await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM trip_understanding_jobs
                WHERE understanding_id=$1 AND job_type='SUPPLEMENT' AND status IN ('QUEUED','RUNNING'))""", resource.understanding_id):
                raise SupplementRejectedError("SUPPLEMENT_RUNNING")
            job_id = str(uuid4())
            await conn.execute("""INSERT INTO trip_understanding_jobs(job_id,understanding_id,revision,job_type,status,
                input_hash,supplement_round,base_public_resource_id,base_etag,base_owner_user_id,base_anonymous_session_id,
                available_at,created_at,updated_at) VALUES($1,$2,$3,'SUPPLEMENT','QUEUED',$4,$5,$6,$7,$8,$9,$10,$10,$10)""",
                job_id, resource.understanding_id, aggregate["current_revision"], current["content_hash"], count + 1,
                resource.public_resource_id, expected_etag, aggregate["owner_user_id"], aggregate["anonymous_session_id"], checked)
            await conn.execute("UPDATE trip_understanding_sources SET supplement_requests=supplement_requests+1 WHERE source_id=$1", current["source_id"])
            view = SupplementStateView(status="QUEUED", message="正在准备补全，现有行程可以继续查看", job_id=job_id,
                base_etag=expected_etag, available_actions=["CANCEL"])
            await conn.execute("""INSERT INTO trip_understanding_idempotency_records(scope,key_hash,request_hash,state,response_status,response_json,
                response_headers_json,created_at,completed_at) VALUES($1,$2,$3,'COMPLETED',202,$4::jsonb,'{}'::jsonb,$5,$5)""",
                scope, key, request_hash, view.model_dump_json(), checked)
            return view

    async def load_supplement_work(self, job, *, now):
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"trip-understanding-resource:{job.understanding_id}")
            row, aggregate, source, _ = await lock_supplement(conn, job, now=now)
            if row["supplement_dispatched_at"] is not None:
                raise SupplementRejectedError("LEASE_TAKEOVER_UNKNOWN_OUTCOME")
            saved = await conn.fetchrow("""SELECT r.proposal_json,p.public_json FROM trip_understanding_revisions r
                JOIN trip_understanding_results p ON p.understanding_id=r.understanding_id AND p.revision=r.revision
                WHERE r.understanding_id=$1 AND r.revision=$2""", job.understanding_id, job.revision)
            cipher = self._get_source_cipher()
            if cipher.key_ref != source["encryption_key_ref"] or source["source_type"] != "TEXT":
                raise SourceUnavailableError("supplement source format unavailable")
            text = cipher.decrypt(bytes(source["encrypted_content"]), source_id=source["source_id"], content_hash=job.input_hash)
            plan = open_source_plan(cipher, _json(saved["proposal_json"]), source_id=source["source_id"],
                source_hash=job.input_hash, envelope=source["encrypted_content"])
            if plan is None or hashlib.sha256(text.encode()).hexdigest() != job.input_hash:
                raise SourceUnavailableError("supplement source context unavailable")
            return SupplementWork(text, plan, UserFacingTripResult.model_validate(_json(saved["public_json"])),
                PublicResourceRecord(understanding_id=job.understanding_id, public_resource_id=aggregate["public_resource_id"],
                    state=aggregate["state"], current_result_id=aggregate["current_result_id"],
                    ownership="ACCOUNT" if aggregate["owner_user_id"] else "ANONYMOUS"), row["base_etag"])

    async def reserve_supplement_call(self, job, *, now):
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"trip-understanding-resource:{job.understanding_id}")
            row, _, source, checked = await lock_supplement(conn, job, now=now)
            if row["supplement_dispatched_at"] is not None:
                raise SupplementRejectedError("SUPPLEMENT_ALREADY_DISPATCHED")
            if source["inference_calls_remaining"] <= 0:
                raise InferenceAllowanceExceeded("MODEL_CALL_BUDGET_EXHAUSTED")
            from app.trip_understanding.execution_repository import source_budget_seconds
            deadline = source["inference_deadline_at"] or min(checked + timedelta(seconds=source_budget_seconds(source)), source["retention_until"])
            if deadline <= checked:
                raise InferenceAllowanceExceeded("MODEL_CALL_DEADLINE_EXCEEDED")
            await conn.execute("UPDATE trip_understanding_sources SET inference_calls_remaining=inference_calls_remaining-1,inference_deadline_at=$2 WHERE source_id=$1",
                source["source_id"], deadline)
            await conn.execute("UPDATE trip_understanding_jobs SET supplement_dispatched_at=$2 WHERE job_id=$1", job.job_id, checked)
            return max(0, (deadline - await conn.fetchval("SELECT clock_timestamp()")).total_seconds())

    async def fail_supplement_job(self, job, *, category, now, provider_binding=None):
        # A stale base may still terminate its own job; it never changes a
        # successful resource, its version, or its available inference budget.
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.fetchrow("SELECT job_id FROM trip_understanding_jobs WHERE job_id=$1 FOR UPDATE", job.job_id)
            checked = await conn.fetchval("SELECT GREATEST($1::timestamptz,clock_timestamp())", now)
            await conn.execute("""UPDATE trip_understanding_jobs SET status='FAILED',last_error_category=$5,
                supplement_outcome_json=$6::jsonb,lease_owner=NULL,lease_until=NULL,finished_at=$4,updated_at=$4
                WHERE job_id=$1 AND job_type='SUPPLEMENT' AND status='RUNNING' AND lease_owner=$2 AND attempt=$3 AND lease_until>$4""",
                job.job_id, job.lease_owner, job.attempt, checked, category, json.dumps({"usage": provider_binding or {}}))

    async def complete_supplement_job(self, job, work, patch, *, now, provider_binding):
        commit = SupplementCommit(job, work, patch, provider_binding)
        if not patch.validation.accepted:
            pool = await self._get_pool()
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"trip-understanding-resource:{job.understanding_id}")
                _, _, _, checked = await lock_supplement(conn, job, now=now)
                await finish_supplement(conn, commit, etag=work.base_etag, now=checked)
            return None
        return await self.apply_command(work.resource, SupplementCommitCommand(), expected_etag=work.base_etag,
            idempotency_key=f"supplement-job:{job.job_id}", request_hash=job.input_hash,
            now=now, _supplement=commit)

    async def get_supplement_state(self, resource, *, now):
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            aggregate = await lock_resource(conn, resource.understanding_id, public_resource_id=resource.public_resource_id, now=now)
            source = await conn.fetchrow("""SELECT s.*,r.proposal_json FROM trip_understanding_sources s JOIN trip_understanding_revisions r
                ON r.source_id=s.source_id WHERE r.understanding_id=$1 AND r.revision=$2""", resource.understanding_id, aggregate["current_revision"])
            row = await conn.fetchrow("SELECT * FROM trip_understanding_jobs WHERE understanding_id=$1 AND job_type='SUPPLEMENT' ORDER BY created_at DESC,job_id DESC LIMIT 1", resource.understanding_id)
            checked = await conn.fetchval("SELECT GREATEST($1::timestamptz,clock_timestamp())", now)
            allowed = (source is not None and source["deleted_at"] is None and source["encrypted_content"] is not None
                and source["retention_until"] > checked and "retained_source_plan" in _json(source["proposal_json"])
                and source["inference_calls_remaining"] > 0 and source["supplement_requests"] < 2
                and (source["inference_deadline_at"] is None or source["inference_deadline_at"] > checked))
            actions = (["REQUEST" if source["supplement_requests"] == 0 else "RETRY"] if allowed else [])
            if row is None:
                return SupplementStateView(status="AVAILABLE" if allowed else "UNAVAILABLE",
                    message="可对照原文补全遗漏安排" if allowed else "暂不能自动补全，仍可对照原文手动补充", available_actions=actions)
            outcome = _json(row["supplement_outcome_json"])
            state = {"SUCCEEDED": "APPLIED" if outcome.get("added_count") else "NO_CHANGES",
                "PARTIAL": "APPLIED" if outcome.get("added_count") else "NO_CHANGES"}.get(row["status"], row["status"])
            state = state if state in {"QUEUED", "RUNNING", "APPLIED", "NO_CHANGES", "CANCELLED"} else "FAILED"
            if state in {"QUEUED", "RUNNING"}:
                actions = ["CANCEL"]
            return SupplementStateView(status=state, job_id=row["job_id"], base_etag=row["base_etag"], result_etag=outcome.get("result_etag"),
                message={"QUEUED":"正在准备补全", "RUNNING":"正在对照原文补全，现有行程可以继续查看", "APPLIED":"补全已保存，请核对新增内容",
                    "NO_CHANGES":"本次没有可安全补入的内容", "FAILED":"本次补全未完成，现有行程已保留", "CANCELLED":"补全已停止，现有行程已保留"}[state],
                added_count=outcome.get("added_count", 0), rejected=outcome.get("rejected", []), available_actions=actions,
                reason=row["last_error_category"])

    async def cancel_supplement(self, resource, job_id, *, now):
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"trip-understanding-resource:{resource.understanding_id}")
            row = await conn.fetchrow("SELECT * FROM trip_understanding_jobs WHERE job_id=$1 AND understanding_id=$2 AND job_type='SUPPLEMENT' FOR UPDATE", job_id, resource.understanding_id)
            await lock_resource(conn, resource.understanding_id, public_resource_id=resource.public_resource_id, now=now)
            if row is None:
                raise ResourceNotFoundError("supplement not found")
            checked = await conn.fetchval("SELECT GREATEST($1::timestamptz,clock_timestamp())", now)
            await conn.execute("""UPDATE trip_understanding_jobs SET status='CANCELLED',lease_owner=NULL,lease_until=NULL,
                last_error_category='USER_CANCELLED',finished_at=$2,updated_at=$2 WHERE job_id=$1 AND status IN ('QUEUED','RUNNING')""", job_id, checked)
        return await self.get_supplement_state(resource, now=now)
