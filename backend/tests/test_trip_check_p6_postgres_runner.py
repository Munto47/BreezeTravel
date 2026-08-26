from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from evals.trip_check_v1.p6.contracts_v1 import P5_GATE_MANIFEST_HASH, P6ContractError, digest
from evals.trip_check_v1.p6.postgres_runner import (
    G2_TEST_NODES,
    _database_readback,
    migration_fingerprint,
    run_postgres_gate,
)


def _spec(tmp_path: Path, repo_root: Path) -> Path:
    migration_sha, _ = migration_fingerprint(repo_root)
    value = {
        "schema_version": "trip-check-p6-candidate-run-spec-v1",
        "subject_commit": "1" * 40,
        "upstream_ref": "origin/codex/trip-check-p6-candidate-evidence",
        "upstream_commit": "1" * 40,
        "dirty_tree": False,
        "p5_gate_manifest_hash": P5_GATE_MANIFEST_HASH,
        "scope": {
            "cities": ["北京", "上海", "杭州"],
            "single_city": True,
            "trip_days": {"min": 2, "max": 5},
            "group_size": {"min": 2, "max": 5},
            "input_types": ["TEXT", "SCREENSHOT"],
        },
        "bindings": {
            "config_sha256": "2" * 64,
            "ocr_dataset_manifest_sha256": "3" * 64,
            "model_manifest_sha256": "4" * 64,
            "rule_manifest_sha256": "5" * 64,
            "snapshot_manifest_sha256": "6" * 64,
            "migration_manifest_sha256": migration_sha,
        },
        "provider_live_matrix": {
            "amap_route_calls": 12,
            "qweather_forecast_calls": 3,
            "qweather_alert_calls": 3,
            "max_calls": 18,
            "retry_budget": 0,
            "fixture_fallback_required_zero": True,
        },
        "database": {
            "engine": "postgresql",
            "isolated": True,
            "required_migration": "024_advice_bundles.sql",
            "migration_hash_readback_required": True,
        },
        "public_candidate": {
            "base_url": "https://www.breezetravel.cn",
            "controlled_snapshot_only": True,
            "health_path": "/health",
            "evidence_path": "/api/evidence/latest",
        },
        "evidence_root": (
            "D:\\munto\\code\\claudeProject\\agentTravel-p6-artifacts"
            "\\p6-candidate\\" + ("1" * 40)
        ),
        "human_evidence": False,
    }
    value["run_spec_hash"] = digest(value)
    path = tmp_path / "candidate_run_spec.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _completed(junit_path: Path, *, failures: int = 0) -> subprocess.CompletedProcess[str]:
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    junit_path.write_text(
        (
            f'<testsuite tests="{len(G2_TEST_NODES)}" failures="{failures}" '
            'errors="0" skipped="0"></testsuite>'
        ),
        encoding="utf-8",
    )
    return subprocess.CompletedProcess([], 0 if failures == 0 else 1, "ok", "")


def test_g2_database_readback_strips_postgres_inet_cidr(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConnection:
        async def fetchrow(self, query: str) -> dict[str, str]:
            assert "host(inet_server_addr()) AS server_address" in query
            return {
                "database_name": "postgres",
                "server_version_num": "160012",
                "server_address": "127.0.0.1",
            }

        async def close(self) -> None:
            return None

    async def connect(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return FakeConnection()

    monkeypatch.setattr("evals.trip_check_v1.p6.postgres_runner.asyncpg.connect", connect)

    assert asyncio.run(_database_readback("postgresql://tester:secret@127.0.0.1:55433/postgres")) == {
        "database_name": "postgres",
        "server_address_class": "LOOPBACK",
        "server_version_num": "160012",
    }


def test_g2_runner_emits_bound_receipt(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[2]
    spec_path = _spec(tmp_path, repo_root)
    log_root = tmp_path / "logs"

    def command_runner(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        junit_arg = next(item for item in args[0] if item.startswith("--junitxml="))
        return _completed(Path(junit_arg.split("=", 1)[1]))

    receipt = asyncio.run(
        run_postgres_gate(
            candidate_run_spec_path=spec_path,
            output_root=tmp_path / "output",
            log_root=log_root,
            repo_root=repo_root,
            database_admin_url="postgresql://tester:secret@127.0.0.1:55433/postgres",
            formal=False,
            command_runner=command_runner,
            database_readback={
                "database_name": "postgres",
                "server_address_class": "LOOPBACK",
                "server_version_num": "160004",
            },
        )
    )
    assert receipt["status"] == "PASS"
    assert receipt["metrics"]["postgres_test_count"] == len(G2_TEST_NODES)
    assert "secret" not in (log_root / "pytest.stdout.log").read_text(encoding="utf-8")


def test_g2_runner_rejects_failed_or_skipped_matrix(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[2]
    spec_path = _spec(tmp_path, repo_root)

    def command_runner(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        junit_arg = next(item for item in args[0] if item.startswith("--junitxml="))
        return _completed(Path(junit_arg.split("=", 1)[1]), failures=1)

    with pytest.raises(P6ContractError, match="P6_G2_TEST_MATRIX_FAILED"):
        asyncio.run(
            run_postgres_gate(
                candidate_run_spec_path=spec_path,
                output_root=tmp_path / "output",
                log_root=tmp_path / "logs",
                repo_root=repo_root,
                database_admin_url="postgresql://tester:secret@127.0.0.1:55433/postgres",
                formal=False,
                command_runner=command_runner,
                database_readback={
                    "database_name": "postgres",
                    "server_address_class": "LOOPBACK",
                    "server_version_num": "160004",
                },
            )
        )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://tester:secret@db.example.com/postgres",
        "postgresql://tester:secret@127.0.0.1:55433/application",
        "postgresql://tester@127.0.0.1:55433/postgres",
    ],
)
def test_g2_runner_rejects_non_isolated_admin_url(tmp_path: Path, url: str) -> None:
    repo_root = Path(__file__).parents[2]
    with pytest.raises(P6ContractError, match="P6_G2_DATABASE_NOT_ISOLATED"):
        asyncio.run(
            run_postgres_gate(
                candidate_run_spec_path=_spec(tmp_path, repo_root),
                output_root=tmp_path / "output",
                log_root=tmp_path / "logs",
                repo_root=repo_root,
                database_admin_url=url,
                formal=False,
                database_readback={
                    "database_name": "postgres",
                    "server_address_class": "LOOPBACK",
                    "server_version_num": "160004",
                },
            )
        )
