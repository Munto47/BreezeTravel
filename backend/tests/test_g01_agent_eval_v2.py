from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evals.agent_gate_v1.path_security import ArtifactPathError
from evals.trip_text_cards_agent_v2.annotations import (
    AgentAnnotationValidationError,
    _case_semantics,
    build_blank_agent_work_packet,
    verify_agent_adjudication,
)
from evals.trip_text_cards_agent_v2.contracts import (
    AgentCaseAnnotation,
    AgentMentionAnnotation,
    ProviderReceiptIndex,
    ProviderRuntimeEffectReceipt,
    agent_input_bundle_sha256,
)
from evals.trip_text_cards_agent_v2.split_loader import (
    AgentSplitValidationError,
    load_agent_split,
)
from evals.trip_text_cards_v1.contracts import (
    CanonicalPlaceLabel,
    CaseAnnotation,
    MentionAnnotation,
    PredictedMention,
    TextCardPrediction,
    canonical_sha256,
)
from evals.trip_text_cards_v1.scorer import ScoringError, score_predictions
from evals.trip_text_cards_v1.validator import load_cases
from scripts.export_g01_amap_live_receipts import (
    _effect_models,
    build_source_only_catalog,
    extract_source_place_candidates,
)
from scripts.export_g01_qwen_live_receipts import _json_object
from scripts.run_qwen_model_predictions import _output_targets
from scripts.score_g01_agent_dev_validation import validate_prediction_run


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DATA_ROOT = BACKEND_ROOT / "eval_data" / "trip_text_cards_v1"
AGENT_ROOT = BACKEND_ROOT / "eval_data" / "trip_text_cards_agent_v2"
PROVIDER_BINDING = "9" * 64
PROMPT_HASH = hashlib.sha256((AGENT_ROOT / "prompts" / "reference.md").read_bytes()).hexdigest()
SCHEMA_HASH = hashlib.sha256((AGENT_ROOT / "agent_annotation.schema.json").read_bytes()).hexdigest()


def _git_value(format_value: str, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "show", "-s", f"--format={format_value}", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _unit_candidate_ref() -> tuple[str, str]:
    """Create an unreferenced test commit without touching the real index or refs.

    The production verifier intentionally reads frozen prompts and schemas from
    the candidate commit.  During first-add development those files are not in
    HEAD yet, so the unit fixture uses a temporary index and a dangling commit
    containing the exact worktree contract bytes.
    """

    relative_paths = [
        "backend/eval_data/trip_text_cards_agent_v2/prompts/reference.md",
        "backend/eval_data/trip_text_cards_agent_v2/prompts/adjudication.md",
        "backend/eval_data/trip_text_cards_agent_v2/agent_annotation.schema.json",
        "backend/eval_data/trip_text_cards_agent_v2/agent_adjudication.schema.json",
    ]
    with tempfile.TemporaryDirectory(prefix="g01-agent-unit-index-") as temporary:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_INDEX_FILE": str(Path(temporary) / "index"),
                "GIT_AUTHOR_NAME": "BreezeTravel unit fixture",
                "GIT_AUTHOR_EMAIL": "unit-fixture@example.invalid",
                "GIT_COMMITTER_NAME": "BreezeTravel unit fixture",
                "GIT_COMMITTER_EMAIL": "unit-fixture@example.invalid",
                "GIT_AUTHOR_DATE": "2026-08-28T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2026-08-28T00:00:00+00:00",
            }
        )
        base_command = ["git", "-C", str(REPOSITORY_ROOT)]
        subprocess.run(
            [*base_command, "read-tree", "HEAD"],
            check=True,
            capture_output=True,
            env=environment,
        )
        subprocess.run(
            [*base_command, "add", "--", *relative_paths],
            check=True,
            capture_output=True,
            env=environment,
        )
        tree = subprocess.run(
            [*base_command, "write-tree"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.strip()
        subject = subprocess.run(
            [*base_command, "commit-tree", tree, "-p", "HEAD"],
            input="G01 agent contract unit fixture\n",
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.strip()
    return subject, tree


SUBJECT, TREE = _unit_candidate_ref()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def test_qwen_application_export_normalizes_asyncpg_jsonb_objects() -> None:
    assert _json_object({"mode": "LIVE"}, label="binding") == {"mode": "LIVE"}
    assert _json_object('{"mode":"LIVE"}', label="binding") == {"mode": "LIVE"}
    with pytest.raises(ValueError, match="must be a JSON object"):
        _json_object('["LIVE"]', label="binding")
    with pytest.raises(ValueError, match="not valid JSON"):
        _json_object("{", label="binding")


def test_qwen_prediction_runner_preflights_external_targets_before_calls(
    tmp_path: Path,
) -> None:
    prediction, summary = _output_targets(
        tmp_path,
        candidate_commit="a" * 40,
        role="LOW_LATENCY_CANDIDATE",
    )

    assert prediction.parent == tmp_path
    assert summary.parent == tmp_path
    prediction.write_text("occupied\n", encoding="utf-8")
    with pytest.raises(ArtifactPathError, match="already exists"):
        _output_targets(
            tmp_path,
            candidate_commit="a" * 40,
            role="LOW_LATENCY_CANDIDATE",
        )
    with pytest.raises(ArtifactPathError, match="parent must already exist"):
        _output_targets(
            tmp_path / "missing",
            candidate_commit="b" * 40,
            role="LOW_LATENCY_CANDIDATE",
        )


def test_non_place_reference_spans_do_not_count_as_missing_activity_roles() -> None:
    source = load_cases(DATA_ROOT)["dev"][0]
    place = "故宫博物院"
    place_start = source.input_text.index(place)
    url_start = source.input_text.index("https://")
    url_end = source.input_text.index("，说明句", url_start)
    destination_start = source.input_text.index("北京")
    gold = AgentCaseAnnotation(
        case_id=source.case_id,
        source_sha256=source.normalized_input_sha256,
        destination_name="北京",
        destination_basis="EXPLICIT",
        destination_evidence_span_start=destination_start,
        destination_evidence_span_end=destination_start + 2,
        destination_evidence_raw_text="北京",
        mentions=[
            AgentMentionAnnotation(
                mention_id="place-optional",
                span_start=place_start,
                span_end=place_start + len(place),
                raw_text=place,
                semantic_kind="PLACE",
                role="OPTIONAL",
                place_boundary_status="VERIFIED_ATOMIC",
                place_boundary_basis="SOURCE_VERBATIM_ATOMIC",
                atomic_place_name=place,
                executable_place=False,
            ),
            AgentMentionAnnotation(
                mention_id="url-reference",
                span_start=url_start,
                span_end=url_end,
                raw_text=source.input_text[url_start:url_end],
                semantic_kind="URL",
                role="REFERENCE",
                place_boundary_status="NONE",
                place_boundary_basis="NONE",
                executable_place=False,
            ),
        ],
    )
    prediction = TextCardPrediction(
        case_id=source.case_id,
        source_sha256=source.normalized_input_sha256,
        destination_name="北京",
        provider_binding={},
        mentions=[
            PredictedMention(
                span_start=place_start,
                span_end=place_start + len(place),
                raw_text=place,
                role="OPTIONAL",
                atomic_place_name=place,
                eligible_for_place_search=False,
                resolution_status="NOT_ELIGIBLE",
            )
        ],
        public_result={},
        measurement_scope="LOCAL_PIPELINE_ONLY",
    )

    score = score_predictions(
        source_cases=[source],
        gold_cases=[gold],
        predictions=[prediction],
    )

    assert score["role_metrics"]["OPTIONAL"]["f1"] == 1
    assert score["role_metrics"]["REFERENCE"]["fn"] == 0


def test_confirmation_threshold_uses_deep_city_and_reports_other_city_burden() -> None:
    cases = load_cases(DATA_ROOT)["dev"]
    deep_source = next(case for case in cases if case.cohort == "DEEP_CITY")
    other_source = next(case for case in cases if case.cohort == "OTHER_CITY")

    def annotation(source, names: list[str], city: str) -> CaseAnnotation:
        mentions = []
        for index, name in enumerate(names, start=1):
            start = source.input_text.index(name)
            mentions.append(
                MentionAnnotation(
                    mention_id=f"gold-{index}",
                    span_start=start,
                    span_end=start + len(name),
                    raw_text=name,
                    semantic_kind="PLACE",
                    role="PLANNED",
                    day_index=1,
                    atomic_place_name=name,
                    executable_place=True,
                    canonical_place=CanonicalPlaceLabel(
                        place_id=f"provider-place-{city}-{index}",
                        name=name,
                        city=city,
                        category="景点",
                        authority="HUMAN_VERIFIED_PROVIDER_RECEIPT",
                        receipt_ref=f"external-provider-receipt-{city}-{index}",
                    ),
                )
            )
        return CaseAnnotation(
            case_id=source.case_id,
            source_sha256=source.normalized_input_sha256,
            destination_name=city,
            mentions=mentions,
        )

    deep_gold = annotation(deep_source, ["故宫博物院"], "北京")
    other_names = [
        "武侯祠",
        "锦里古街",
        "人民公园",
        "宽窄巷子",
        "成都博物馆",
        "东郊记忆",
    ]
    other_gold = annotation(other_source, other_names, "成都")
    deep_label = deep_gold.mentions[0].canonical_place
    assert deep_label is not None
    deep_prediction = TextCardPrediction(
        case_id=deep_source.case_id,
        source_sha256=deep_source.normalized_input_sha256,
        destination_name="北京",
        provider_binding={},
        mentions=[
            PredictedMention(
                span_start=deep_gold.mentions[0].span_start,
                span_end=deep_gold.mentions[0].span_end,
                raw_text="故宫博物院",
                role="PLANNED",
                day_index=1,
                atomic_place_name="故宫博物院",
                eligible_for_place_search=True,
                resolution_status="AUTO_MATCHED",
                canonical_place_id=deep_label.place_id,
                canonical_city=deep_label.city,
                canonical_category=deep_label.category,
            )
        ],
        public_result={},
        measurement_scope="LOCAL_PIPELINE_ONLY",
    )
    other_prediction = TextCardPrediction(
        case_id=other_source.case_id,
        source_sha256=other_source.normalized_input_sha256,
        destination_name="成都",
        provider_binding={},
        mentions=[
            PredictedMention(
                span_start=mention.span_start,
                span_end=mention.span_end,
                raw_text=mention.raw_text,
                role="PLANNED",
                day_index=1,
                atomic_place_name=mention.raw_text,
                eligible_for_place_search=True,
                resolution_status="NEEDS_CONFIRMATION",
            )
            for mention in other_gold.mentions
        ],
        public_result={},
        measurement_scope="LOCAL_PIPELINE_ONLY",
    )

    score = score_predictions(
        source_cases=[deep_source, other_source],
        gold_cases=[deep_gold, other_gold],
        predictions=[deep_prediction, other_prediction],
    )

    assert score["human_confirmation_count"] == {
        "population": "DEEP_CITY",
        "case_count": 1,
        "gold_executable_count": 1,
        "total": 0,
        "median": 0.0,
        "p90": 0.0,
        "max": 0,
    }
    assert score["other_city_confirmation_required_count"] == {
        "population": "OTHER_CITY",
        "case_count": 1,
        "gold_executable_count": 6,
        "auto_match_count": 0,
        "correct_auto_match_count": 0,
        "total": 6,
        "median": 6.0,
        "p90": 6.0,
        "max": 6,
    }


def _attestation(task_id: str, role: str, *, start_minute: int = 0) -> dict[str, object]:
    started = datetime(2026, 8, 28, 1, start_minute, tzinfo=UTC)
    completed = started + timedelta(seconds=30)
    return {
        "task_id": task_id,
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "prompt_sha256": PROMPT_HASH,
        "input_bundle_sha256": "b" * 64,
        "output_schema_sha256": SCHEMA_HASH,
        "subject_commit": SUBJECT,
        "subject_tree": TREE,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "frozen_at": (completed + timedelta(seconds=1)).isoformat(),
        "context_fork": "none",
        "isolated_context": True,
        "human_evidence": False,
        "saw_prior_verdict": False,
        "task_role": role,
        "saw_peer_output_before_submission": False,
        "saw_candidate_predictions_before_submission": False,
        "peer_output_visibility": "NONE",
        "candidate_output_visibility": "NONE",
        "raw_output_storage": "REPOSITORY_EXTERNAL",
        "provider_receipts_used": True,
    }


def test_source_only_provider_catalog_covers_all_dev_validation_roles_without_blind() -> None:
    cases = load_cases(DATA_ROOT)
    ordinary = [*cases["dev"], *cases["validation"]]

    for case in ordinary:
        names = extract_source_place_candidates(case.input_text)
        assert len(names) == 10
        assert len(set(names)) == 10
        assert all(name in case.input_text for name in names)
        assert all("http" not in name.casefold() for name in names)

    catalog = build_source_only_catalog(ordinary)
    assert {"北京", "上海", "杭州", "成都", "西安", "广州", "南京"} <= set(catalog)
    assert "故宫博物院" in catalog["北京"]
    assert "北京环球影城" in catalog["北京"]
    assert "王府井地铁站" in catalog["北京"]


def test_application_table_export_preserves_other_city_zero_call_as_unresolved() -> None:
    completed = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
    rows = [
        {
            "activity_id": "application-activity-0001",
            "role": "PLANNED",
            "eligible_for_place_search": True,
            "atomic_place_name": "中山陵",
            "resolution_status": "NEEDS_CONFIRMATION",
            "canonical_place_id": None,
            "resolver_receipt_json": {
                "provider": "AMAP_POI_V2",
                "execution_mode": "LIVE",
                "status": "BASIC_CITY_CONFIRMATION_REQUIRED",
                "city": "南京",
                "query_sha256": "1" * 64,
                "endpoint_sha256": "2" * 64,
                "external_calls": 0,
                "raw_provider_response_retained": False,
            },
            "created_at": completed,
        }
    ]

    database, http, runtime, receipts = _effect_models(
        rows,
        provider_binding_sha256=PROVIDER_BINDING,
    )

    assert database[0].external_call_count == 0
    assert http[0].provider_status == "NOT_CALLED"
    assert http[0].http_status is None
    assert runtime[0].resolution_status == "UNRESOLVED"
    assert runtime[0].queried_city == "南京"
    assert receipts[0].accepted_source_name is None


def test_application_table_export_binds_one_typed_rewrite_as_two_calls() -> None:
    completed = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
    rows = [
        {
            "activity_id": "application-activity-0002",
            "role": "PLANNED",
            "eligible_for_place_search": True,
            "atomic_place_name": "颐和园",
            "resolution_status": "AUTO_MATCHED",
            "canonical_place_id": "B000A00002",
            "resolver_receipt_json": {
                "provider": "AMAP_POI_V2",
                "execution_mode": "LIVE",
                "status": "AUTO_MATCHED",
                "city": "北京",
                "query_sha256": "1" * 64,
                "endpoint_sha256": "2" * 64,
                "request_sha256": "3" * 64,
                "response_sha256": "4" * 64,
                "provider_request_id_sha256": "5" * 64,
                "http_status": 200,
                "observed_at": (
                    completed - timedelta(milliseconds=20)
                ).isoformat(),
                "latency_ms": 20,
                "external_calls": 2,
                "rewrite_count": 1,
                "query_strategy": (
                    "CATEGORY_FILTERED_THEN_UNTYPED_LOCAL_CATEGORY_CHECK"
                ),
                "category_compatible_candidate_count": 1,
                "typecode": "110000",
                "raw_provider_response_retained": False,
            },
            "created_at": completed,
        }
    ]

    database, http, runtime, receipts = _effect_models(
        rows,
        provider_binding_sha256=PROVIDER_BINDING,
    )

    assert database[0].external_call_count == 2
    assert http[0].external_call_count == 2
    assert http[0].provider_status == "SUCCESS"
    assert http[0].http_status == 200
    assert runtime[0].external_call_count == 2
    assert runtime[0].resolution_status == "MATCHED"
    assert receipts[0].accepted_source_name == "颐和园"


def _provider_assets(
    tmp_path: Path,
    raw: str,
) -> tuple[Path, Path, Path, Path, ProviderReceiptIndex]:
    started = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    completed = started + timedelta(seconds=1)
    effect_value = {
        "effect_id": "amap-runtime-effect-0001",
        "effect_key_sha256": "1" * 64,
        "provider": "AMAP",
        "execution_mode": "CONTROLLED_FIXTURE",
        "provider_binding_sha256": PROVIDER_BINDING,
        "request_sha256": "2" * 64,
        "response_sha256": "3" * 64,
        "resolution_status": "MATCHED",
        "queried_source_name": raw,
        "queried_city": "北京",
        "external_call_count": 1,
        "place_id": "B000A00001",
        "name": raw,
        "city": "北京",
        "category": "景点",
        "accepted_source_names": [raw],
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "status": "SUCCEEDED",
        "raw_response_in_repository": False,
    }
    effect = ProviderRuntimeEffectReceipt.model_validate(effect_value)
    database_path = tmp_path / "provider-database-export.json"
    _write(
        database_path,
        {
            "schema_version": "g01-amap-database-export-receipt-v2",
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "candidate_commit": SUBJECT,
            "candidate_tree": TREE,
            "provider_binding_sha256": PROVIDER_BINDING,
            "execution_mode": "CONTROLLED_FIXTURE",
            "source_registry": "CONTROLLED_CONTRACT_FIXTURE",
            "query_sha256": "4" * 64,
            "transaction_snapshot_sha256": "5" * 64,
            "exported_at": (completed + timedelta(seconds=1)).isoformat(),
            "effects": [
                {
                    "effect_id": effect.effect_id,
                    "effect_key_sha256": effect.effect_key_sha256,
                    "provider_binding_sha256": effect.provider_binding_sha256,
                    "request_sha256": effect.request_sha256,
                    "response_sha256": effect.response_sha256,
                    "resolution_status": effect.resolution_status,
                    "external_call_count": effect.external_call_count,
                    "started_at": effect.started_at.isoformat(),
                    "completed_at": effect.completed_at.isoformat(),
                    "persisted_status": "SUCCEEDED",
                }
            ],
        },
    )
    http_path = tmp_path / "provider-http-receipts.json"
    _write(
        http_path,
        {
            "schema_version": "g01-amap-http-receipt-bundle-v2",
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "candidate_commit": SUBJECT,
            "candidate_tree": TREE,
            "provider_binding_sha256": PROVIDER_BINDING,
            "execution_mode": "CONTROLLED_FIXTURE",
            "captured_at": (completed + timedelta(seconds=1)).isoformat(),
            "exchanges": [
                {
                    "effect_id": effect.effect_id,
                    "request_sha256": effect.request_sha256,
                    "response_sha256": effect.response_sha256,
                    "external_call_count": effect.external_call_count,
                    "http_status": 200,
                    "provider_status": "SUCCESS",
                    "completed_at": effect.completed_at.isoformat(),
                    "raw_response_retained": False,
                }
            ],
        },
    )
    runtime_path = tmp_path / "provider-runtime.json"
    _write(
        runtime_path,
        {
            "schema_version": "g01-amap-runtime-receipt-bundle-v2",
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "candidate_commit": SUBJECT,
            "candidate_tree": TREE,
            "provider_binding_sha256": PROVIDER_BINDING,
            "execution_mode": "CONTROLLED_FIXTURE",
            "database_export_receipt_path": str(database_path.resolve()),
            "database_export_receipt_sha256": _sha(database_path),
            "provider_http_receipt_bundle_path": str(http_path.resolve()),
            "provider_http_receipt_bundle_sha256": _sha(http_path),
            "generated_at": (completed + timedelta(seconds=1)).isoformat(),
            "generated_by": "G01_AMAP_CONTROLLED_FIXTURE_EXPORTER",
            "source_runtime": "CONTROLLED_CONTRACT_FIXTURE",
            "evidence_level": "AUTOMATED_TEST",
            "effects": [effect_value],
        },
    )
    record = {
        "receipt_id": "amap-receipt-0001",
        "provider": "AMAP",
        "execution_mode": "CONTROLLED_FIXTURE",
        "provider_binding_sha256": PROVIDER_BINDING,
        "receipt_ref": effect.effect_id,
        "runtime_effect_id": effect.effect_id,
        "runtime_effect_receipt_sha256": canonical_sha256(effect.model_dump(mode="json")),
        "request_sha256": effect.request_sha256,
        "response_sha256": effect.response_sha256,
        "observed_at": effect.completed_at.isoformat(),
        "resolution_status": "MATCHED",
        "queried_source_name": raw,
        "queried_city": "北京",
        "accepted_source_name": raw,
        "authorization_basis": "OWNER_ATTESTED_EXISTING_AUTHORIZATION",
        "raw_response_in_git": False,
        "retention": "REDACTED_MINIMAL",
        "place_id": effect.place_id,
        "name": effect.name,
        "city": effect.city,
        "category": effect.category,
        "accepted_source_names": [raw],
    }
    index_value = {
        "schema_version": "g01-text-card-provider-receipt-index-v2",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "dataset_version": "g01-text-card-dataset-v1",
        "split": "dev",
        "subject_commit": SUBJECT,
        "subject_tree": TREE,
        "provider_binding_sha256": PROVIDER_BINDING,
        "execution_mode": "CONTROLLED_FIXTURE",
        "evidence_level": "AUTOMATED_TEST",
        "runtime_receipt_bundle_sha256": _sha(runtime_path),
        "frozen_at": (completed + timedelta(seconds=2)).isoformat(),
        "receipts": [record],
    }
    index_path = tmp_path / "provider-index.json"
    _write(index_path, index_value)
    return (
        index_path,
        runtime_path,
        database_path,
        http_path,
        ProviderReceiptIndex.model_validate(index_value),
    )


def _case_and_receipt(record: dict[str, object] | None = None):
    source = load_cases(DATA_ROOT)["dev"][0]
    raw = "故宫博物院"
    start = source.input_text.index(raw)
    city_start = source.input_text.index("北京")
    if record is None:
        record = {
            "receipt_id": "amap-receipt-0001",
            "provider": "AMAP",
            "execution_mode": "CONTROLLED_FIXTURE",
            "provider_binding_sha256": PROVIDER_BINDING,
            "receipt_ref": "amap-runtime-effect-0001",
            "runtime_effect_id": "amap-runtime-effect-0001",
            "runtime_effect_receipt_sha256": "6" * 64,
            "request_sha256": "2" * 64,
            "response_sha256": "3" * 64,
            "observed_at": datetime(2026, 8, 28, 0, 0, 1, tzinfo=UTC).isoformat(),
            "resolution_status": "MATCHED",
            "queried_source_name": raw,
            "queried_city": "北京",
            "accepted_source_name": raw,
            "authorization_basis": "OWNER_ATTESTED_EXISTING_AUTHORIZATION",
            "raw_response_in_git": False,
            "retention": "REDACTED_MINIMAL",
        }
    receipt_ref = {
        key: value
        for key, value in record.items()
        if key
        not in {
            "place_id",
            "name",
            "city",
            "category",
            "accepted_source_names",
        }
    }
    case = {
        "case_id": source.case_id,
        "source_sha256": source.normalized_input_sha256,
        "destination_name": "北京",
        "destination_basis": "EXPLICIT",
        "destination_evidence_span_start": city_start,
        "destination_evidence_span_end": city_start + len("北京"),
        "destination_evidence_raw_text": "北京",
        "mentions": [
            {
                "mention_id": "mention-1",
                "span_start": start,
                "span_end": start + len(raw),
                "raw_text": raw,
                "semantic_kind": "PLACE",
                "role": "PLANNED",
                "day_index": 1,
                "place_boundary_status": "VERIFIED_ATOMIC",
                "place_boundary_basis": "PROVIDER_ACCEPTED_EXACT",
                "atomic_place_name": raw,
                "executable_place": True,
                "provider_resolution_receipt": receipt_ref,
                "canonical_place": {
                    "place_id": "B000A00001",
                    "name": raw,
                    "city": "北京",
                    "category": "景点",
                    "authority": "PROVIDER_BOUND_AGENT_REFERENCE",
                    "provider_receipt": receipt_ref,
                },
            }
        ],
    }
    return source, case


def test_agent_schema_contains_no_false_human_attestation() -> None:
    serialized = (AGENT_ROOT / "agent_annotation.schema.json").read_text(encoding="utf-8")
    assert "is_authorized_human" not in serialized
    assert "human_label" not in serialized
    assert "PROVIDER_BOUND_AGENT_REFERENCE" in serialized


def test_agent_reference_requires_provider_exact_boundary_and_keeps_uncertain_cards() -> None:
    _source, case = _case_and_receipt()
    mention = dict(case["mentions"][0])
    mention["raw_text"] = "上午先到故宫博物院然后吃饭"
    mention["atomic_place_name"] = mention["raw_text"]
    with pytest.raises(ValueError, match="receipt query"):
        AgentMentionAnnotation.model_validate(mention)

    mention = dict(case["mentions"][0])
    mention["raw_text"] = "拍照绝佳的故宫博物院"
    mention["span_end"] = mention["span_start"] + len(mention["raw_text"])
    mention["place_boundary_status"] = "UNCERTAIN"
    mention["place_boundary_basis"] = "NONE"
    mention["atomic_place_name"] = None
    mention["executable_place"] = False
    mention["provider_resolution_receipt"] = None
    mention["canonical_place"] = None
    parsed = AgentMentionAnnotation.model_validate(mention)
    assert parsed.canonical_place is None
    assert parsed.provider_resolution_receipt is None
    assert parsed.executable_place is False
    assert parsed.place_boundary_status == "UNCERTAIN"

    mention = dict(case["mentions"][0])
    mention["raw_text"] = "故宫博物院值得拍照"
    mention["atomic_place_name"] = mention["raw_text"]
    mention["span_end"] = mention["span_start"] + len(mention["raw_text"])
    with pytest.raises(ValueError, match="receipt query"):
        AgentMentionAnnotation.model_validate(mention)


def test_unresolved_atomic_planned_place_remains_executable() -> None:
    _source, case = _case_and_receipt()
    mention = dict(case["mentions"][0])
    receipt = dict(mention["provider_resolution_receipt"])
    receipt["resolution_status"] = "UNRESOLVED"
    receipt["accepted_source_name"] = None
    mention["provider_resolution_receipt"] = receipt
    mention["canonical_place"] = None
    mention["place_boundary_basis"] = "SOURCE_VERBATIM_ATOMIC"

    parsed = AgentMentionAnnotation.model_validate(mention)

    assert parsed.executable_place is True
    assert parsed.canonical_place is None
    assert parsed.provider_resolution_receipt is not None
    assert parsed.provider_resolution_receipt.resolution_status == "UNRESOLVED"


def test_destination_basis_and_evidence_participate_in_conflict_fingerprint() -> None:
    _source, case = _case_and_receipt()
    explicit = AgentCaseAnnotation.model_validate(case)
    assumed_value = dict(case)
    assumed_value["destination_basis"] = "SOFT_ASSUMPTION"
    assumed_value["destination_evidence_span_start"] = None
    assumed_value["destination_evidence_span_end"] = None
    assumed_value["destination_evidence_raw_text"] = None
    assumed = AgentCaseAnnotation.model_validate(assumed_value)
    assert _case_semantics(explicit) != _case_semantics(assumed)


def test_blank_agent_packet_contains_frozen_provider_index_and_no_candidate_output(
    tmp_path: Path,
) -> None:
    cases = load_cases(DATA_ROOT)["dev"][:1]
    index_path, _runtime_path, _database_path, _http_path, provider_index = (
        _provider_assets(tmp_path, "故宫博物院")
    )
    packet = build_blank_agent_work_packet(
        split="dev",
        assignment_id="agent-assignment-a",
        source_cases=cases,
        prompt_sha256=PROMPT_HASH,
        provider_receipt_index=provider_index,
        provider_receipt_index_sha256=_sha(index_path),
    )
    assert packet["provider_receipts_present"] is True
    assert packet["provider_receipt_index_sha256"] == _sha(index_path)
    assert packet["candidate_predictions_included"] is False
    assert packet["peer_labels_included"] is False
    with pytest.raises(AgentAnnotationValidationError, match="frozen_blind"):
        build_blank_agent_work_packet(
            split="frozen_blind",
            assignment_id="agent-assignment-a",
            source_cases=cases,
            prompt_sha256=PROMPT_HASH,
            provider_receipt_index=provider_index,
            provider_receipt_index_sha256=_sha(index_path),
        )


def test_agent_adjudication_binds_distinct_tasks_and_runtime_provider_receipt(
    tmp_path: Path,
) -> None:
    index_path, runtime_path, database_path, http_path, provider_index = (
        _provider_assets(tmp_path, "故宫博物院")
    )
    record = provider_index.model_dump(mode="json")["receipts"][0]
    source, case = _case_and_receipt(record)
    input_hash = agent_input_bundle_sha256("dev", [source], _sha(index_path))
    first = {
        "schema_version": "g01-text-card-agent-annotation-bundle-v2",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "dataset_version": "g01-text-card-dataset-v1",
        "assignment_id": "agent-assignment-a",
        "split": "dev",
        "attestation": _attestation("isolated-agent-task-a", "ANNOTATOR_A"),
        "cases": [case],
    }
    first["attestation"]["input_bundle_sha256"] = input_hash
    second = {
        **first,
        "assignment_id": "agent-assignment-b",
        "attestation": _attestation("isolated-agent-task-b", "ANNOTATOR_B"),
    }
    second["attestation"]["input_bundle_sha256"] = input_hash
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    _write(first_path, first)
    _write(second_path, second)

    adjudicator_start = datetime(2026, 8, 28, 1, 2, tzinfo=UTC)
    source_hashes = sorted([_sha(first_path), _sha(second_path)])
    adjudication = {
        "schema_version": "g01-text-card-agent-adjudication-bundle-v2",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "dataset_version": "g01-text-card-dataset-v1",
        "split": "dev",
        "source_assignment_ids": ["agent-assignment-a", "agent-assignment-b"],
        "source_bundle_sha256": source_hashes,
        "attestation": {
            "task_id": "isolated-agent-adjudicator",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "ultra",
            "prompt_sha256": _sha(AGENT_ROOT / "prompts" / "adjudication.md"),
            "input_bundle_sha256": canonical_sha256(
                {
                    "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
                    "candidate_commit": SUBJECT,
                    "candidate_tree": TREE,
                    "provider_receipt_index_sha256": _sha(index_path),
                    "provider_runtime_receipt_bundle_sha256": _sha(runtime_path),
                    "source_bundle_sha256": source_hashes,
                }
            ),
            "output_schema_sha256": _sha(
                AGENT_ROOT / "agent_adjudication.schema.json"
            ),
            "subject_commit": SUBJECT,
            "subject_tree": TREE,
            "started_at": adjudicator_start.isoformat(),
            "completed_at": (adjudicator_start + timedelta(seconds=30)).isoformat(),
            "frozen_at": (adjudicator_start + timedelta(seconds=31)).isoformat(),
            "context_fork": "none",
            "isolated_context": True,
            "human_evidence": False,
            "saw_prior_verdict": False,
            "task_role": "ADJUDICATOR",
            "reviewed_both_frozen_bundles": True,
            "saw_candidate_predictions_before_submission": False,
            "candidate_output_visibility": "NONE",
            "raw_output_storage": "REPOSITORY_EXTERNAL",
        },
        "conflicts": [],
        "agent_reference_cases": [case],
    }
    adjudication_path = tmp_path / "adjudication.json"
    _write(adjudication_path, adjudication)

    kwargs = {
        "split": "dev",
        "source_cases": [source],
        "first_path": first_path,
        "second_path": second_path,
        "adjudication_path": adjudication_path,
        "provider_receipt_index_path": index_path,
        "provider_runtime_receipt_bundle_path": runtime_path,
        "repository_root": REPOSITORY_ROOT,
        "expected_candidate_commit": SUBJECT,
        "expected_candidate_tree": TREE,
        "expected_provider_binding_sha256": PROVIDER_BINDING,
        "expected_runtime_receipt_bundle_sha256": _sha(runtime_path),
        "expected_database_export_receipt_sha256": _sha(database_path),
        "expected_provider_http_receipt_bundle_sha256": _sha(http_path),
        "require_live_provider_evidence": False,
    }
    _adjudicated, receipt = verify_agent_adjudication(**kwargs)
    assert receipt["human_evidence"] is False
    assert receipt["canonical_provider_bound_mentions"] == 1
    assert receipt["live_provider_evidence_verified"] is False

    live_required = {**kwargs, "require_live_provider_evidence": True}
    with pytest.raises(AgentAnnotationValidationError, match="live Provider evidence"):
        verify_agent_adjudication(**live_required)

    original_http = http_path.read_text(encoding="utf-8")
    http_value = json.loads(original_http)
    http_value["exchanges"][0]["response_sha256"] = "a" * 64
    _write(http_path, http_value)
    with pytest.raises(AgentAnnotationValidationError, match="HTTP receipt bundle hash"):
        verify_agent_adjudication(**kwargs)
    http_path.write_text(original_http, encoding="utf-8", newline="")

    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["effects"][0]["city"] = "上海"
    _write(runtime_path, runtime)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["runtime_receipt_bundle_sha256"] = _sha(runtime_path)
    _write(index_path, index)
    kwargs["expected_runtime_receipt_bundle_sha256"] = _sha(runtime_path)
    with pytest.raises(AgentAnnotationValidationError, match="runtime effect"):
        verify_agent_adjudication(**kwargs)


def test_prediction_run_binds_candidate_model_config_provider_and_prediction_bytes(
    tmp_path: Path,
) -> None:
    source = load_cases(DATA_ROOT)["dev"][0]
    provider_binding_path = AGENT_ROOT / "provider_binding.json"
    provider_binding = json.loads(provider_binding_path.read_text(encoding="utf-8"))
    provider_hash = _sha(provider_binding_path)
    model_binding_path = tmp_path / "model-binding.json"
    prompt_path = tmp_path / "prompt.md"
    schema_path = tmp_path / "schema.json"
    config_path = tmp_path / "config.json"
    _write(model_binding_path, {"provider": "QWEN", "model": "qwen-test-snapshot"})
    _write(prompt_path, {"prompt": "frozen test prompt"})
    _write(schema_path, {"type": "object"})
    _write(config_path, {"temperature": 0, "thinking": False})
    prediction_path = tmp_path / "predictions.jsonl"
    prediction = TextCardPrediction.model_validate(
        {
            "schema_version": "g01-text-card-prediction-v1",
            "dataset_version": "g01-text-card-dataset-v1",
            "case_id": source.case_id,
            "source_sha256": source.normalized_input_sha256,
            "destination_name": "北京",
            "provider_binding": provider_binding,
            "mentions": [],
            "public_result": {},
            "measurement_scope": "LOCAL_PIPELINE_ONLY",
            "first_progress_ms": None,
            "cards_ready_ms": None,
        }
    )
    _write(prediction_path, prediction.model_dump(mode="json"))
    city_start = source.input_text.index("北京")
    destination_prediction = {
        "case_id": source.case_id,
        "destination_name": "北京",
        "destination_basis": "EXPLICIT",
        "evidence_span_start": city_start,
        "evidence_span_end": city_start + 2,
        "evidence_raw_text": "北京",
    }
    combined_output = {
        "schema_version": "g01-agent-inference-case-output-v2",
        "case_id": source.case_id,
        "source_sha256": source.normalized_input_sha256,
        "text_card_prediction": prediction.model_dump(mode="json"),
        "destination_prediction": destination_prediction,
    }
    inference_outputs_path = tmp_path / "inference-outputs.jsonl"
    _write(inference_outputs_path, combined_output)
    inference_path = tmp_path / "inference-receipts.json"
    inference_started = datetime(2026, 8, 28, 1, 59, tzinfo=UTC)
    inference_completed = inference_started + timedelta(seconds=1)
    bindings = {
        "candidate_commit": SUBJECT,
        "candidate_tree": TREE,
        "model_binding_sha256": _sha(model_binding_path),
        "prompt_sha256": _sha(prompt_path),
        "schema_sha256": _sha(schema_path),
        "config_sha256": _sha(config_path),
        "provider_binding_sha256": provider_hash,
    }
    _write(
        inference_path,
        {
            "schema_version": "g01-qwen-inference-receipt-bundle-v2",
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "dataset_version": "g01-text-card-dataset-v1",
            "split": "dev",
            "candidate_commit": SUBJECT,
            "candidate_tree": TREE,
            "provider": "QWEN",
            "execution_mode": "CONTROLLED_FIXTURE",
            "evidence_level": "AUTOMATED_TEST",
            "region": "test-region",
            "endpoint_sha256": "7" * 64,
            "exact_model_id": "qwen-test-snapshot",
            **bindings,
            "predictions_sha256": _sha(prediction_path),
            "inference_outputs_sha256": _sha(inference_outputs_path),
            "generated_at": (inference_completed + timedelta(seconds=1)).isoformat(),
            "generated_by": "G01_QWEN_CONTROLLED_FIXTURE_EXPORTER",
            "raw_request_or_response_in_repository": False,
            "effects": [
                {
                    "effect_id": "qwen-inference-effect-0001",
                    "case_id": source.case_id,
                    "input_sha256": source.normalized_input_sha256,
                    "request_sha256": "8" * 64,
                    "response_sha256": "9" * 64,
                    "output_sha256": canonical_sha256(combined_output),
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "latency_ms": 321.0,
                    "repair_call_count": 0,
                    "started_at": inference_started.isoformat(),
                    "completed_at": inference_completed.isoformat(),
                    "status": "SUCCEEDED",
                }
            ],
        },
    )
    bindings["inference_receipt_bundle_sha256"] = _sha(inference_path)
    envelope = {
        "schema_version": "g01-text-card-agent-prediction-run-v2",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "dataset_version": "g01-text-card-dataset-v1",
        "split": "dev",
        **bindings,
        "predictions_sha256": _sha(prediction_path),
        "inference_outputs_sha256": _sha(inference_outputs_path),
        "generated_at": datetime(2026, 8, 28, 2, tzinfo=UTC).isoformat(),
        "destination_predictions": [destination_prediction],
    }
    envelope_path = tmp_path / "prediction-envelope.json"
    _write(envelope_path, envelope)
    validation_kwargs = {
        "prediction_path": prediction_path,
        "inference_outputs_path": inference_outputs_path,
        "envelope_path": envelope_path,
        "repository_root": REPOSITORY_ROOT,
        "split": "dev",
        "expected_bindings": bindings,
        "inference_receipt_bundle_path": inference_path,
        "model_binding_artifact_path": model_binding_path,
        "prompt_artifact_path": prompt_path,
        "schema_artifact_path": schema_path,
        "config_artifact_path": config_path,
    }
    parsed, predictions, inference, inference_outputs = validate_prediction_run(
        **validation_kwargs
    )
    assert parsed.candidate_commit == SUBJECT
    assert len(predictions) == 1
    assert inference.effects[0].case_id == source.case_id
    assert inference_outputs[0].destination_prediction.destination_basis == "EXPLICIT"

    wrong = dict(bindings)
    wrong["candidate_commit"] = "f" * 40
    with pytest.raises(ScoringError, match="candidate_commit"):
        validate_prediction_run(
            **{**validation_kwargs, "expected_bindings": wrong},
        )

    mismatched_envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    mismatched_envelope["destination_predictions"][0]["destination_name"] = "上海"
    _write(envelope_path, mismatched_envelope)
    with pytest.raises(ScoringError, match="strict combined-output projection"):
        validate_prediction_run(**validation_kwargs)
    _write(envelope_path, envelope)

    original_inference = inference_path.read_text(encoding="utf-8")
    inference_value = json.loads(original_inference)
    inference_value["effects"][0]["response_sha256"] = "a" * 64
    _write(inference_path, inference_value)
    with pytest.raises(ScoringError, match="inference receipt bundle artifact hash"):
        validate_prediction_run(**validation_kwargs)
    inference_path.write_text(original_inference, encoding="utf-8", newline="")

    original_combined = inference_outputs_path.read_text(encoding="utf-8")
    combined_changed = json.loads(original_combined)
    combined_changed["destination_prediction"]["destination_basis"] = "SOFT_ASSUMPTION"
    combined_changed["destination_prediction"]["evidence_span_start"] = None
    combined_changed["destination_prediction"]["evidence_span_end"] = None
    combined_changed["destination_prediction"]["evidence_raw_text"] = None
    _write(inference_outputs_path, combined_changed)
    with pytest.raises(ScoringError, match="combined inference output hash"):
        validate_prediction_run(**validation_kwargs)
    inference_outputs_path.write_text(original_combined, encoding="utf-8", newline="")

    prediction_path.write_text(prediction_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ScoringError, match="prediction file hash"):
        validate_prediction_run(**validation_kwargs)


def test_split_loader_opens_only_the_requested_non_blind_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    original_read_bytes = Path.read_bytes

    def recording_read_bytes(path: Path) -> bytes:
        opened.append(path.name)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)
    cases, receipt = load_agent_split(DATA_ROOT, "dev")
    assert len(cases) == 54
    assert opened == ["dev.inputs.jsonl"]
    assert receipt.blind_inputs_read == 0
    assert receipt.blind_truth_read == 0
    with pytest.raises(AgentSplitValidationError, match="cannot open frozen_blind"):
        load_agent_split(DATA_ROOT, "frozen_blind")
