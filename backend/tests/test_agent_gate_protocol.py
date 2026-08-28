from __future__ import annotations

import hashlib
import io
import inspect
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.agent_gate_v1.authority import (
    AnchoredAuthorityPolicy,
    AuthorityPolicyError,
    _stable_program_facts,
    _validate_generation_one_activation,
    compute_git_blob_bundle_hash,
    compute_git_tree_bundle_hash_excluding,
    compute_public_key_set_sha256,
    load_anchored_authority_policy,
    load_current_goal_binding,
    load_current_goal_document_state,
)
from evals.agent_gate_v1.automation_isolation import (
    AutomationIsolationError,
    _docker_environment,
    _materialize_candidate_git_context,
    _run_docker,
    ensure_isolated_candidate_image,
    save_isolated_candidate_image,
)
from evals.agent_gate_v1.component_verifiers import (
    ComponentVerificationError,
    load_strict_component_receipt,
    verify_strict_component_receipt,
)
from evals.agent_gate_v1.final_gate import verify_agent_gate_pass
from evals.agent_gate_v1.component_builders import (
    ComponentBuildError,
    build_automated_product_component,
    build_live_provider_component,
    build_multi_agent_panel_component,
    build_sealed_agent_blind_component,
)
from evals.agent_gate_v1.contracts import (
    AgentGateAuthorityManifest,
    AuthorityActivationReadinessReceipt,
    AuthorityAnchorReceipt,
    AgentGatePassReceipt,
    AgentGateReviewReceipt,
    AutomatedProductExecutionManifest,
    AutomatedProductGateContract,
    AutomatedProductGateReceipt,
    DetachedAuthoritySignature,
    CurrentGoalBinding,
    SealedAgentBlindMintReceipt,
    SealedAgentBlindScoreReceipt,
)
from evals.agent_gate_v1.custody import (
    SealedBlindCustodyError,
    consume_minted_run,
    initialize_custody_registry,
    mint_sealed_blind_run,
    read_run_state,
    register_authority_anchor,
    register_goal_gate_pass,
    recover_registered_goal_gate_pass,
    require_predecessor_goal_pass,
)
from evals.agent_gate_v1.path_security import (
    ArtifactPathError,
    publish_external_stream_exclusive,
    require_external_existing,
    require_external_target,
)
from evals.agent_gate_v1.development_signing import (
    generate_authority_keypair,
    sign_payload_for_development,
)
from evals.agent_gate_v1.signing import (
    unsigned_payload,
    verify_payload_signature,
)
from scripts.generate_agent_gate_contracts import generate
from scripts.score_g01_sealed_agent_blind import tranche_commitment_sha256
from evals.trip_text_cards_agent_v2.contracts import (
    InferenceDatabaseExportReceipt,
    InferenceRuntimeReceiptBundle,
    ProviderDatabaseExportReceipt,
)
from evals.trip_text_cards_agent_v2.annotations import (
    AgentAnnotationValidationError,
    _require_frozen_provider_binding,
)
from evals.agent_gate_v1.live_export import (
    AMAP_EFFECT_QUERY,
    LiveEvidenceExportError,
    QWEN_EFFECT_QUERY,
    export_live_lane,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _docker_image_archive_bytes(
    *,
    extra_manifest_entry: bool = False,
    invalid_layer_binding: bool = False,
    repo_tags: list[str] | None = None,
) -> tuple[bytes, str]:
    layer_content = b"layer bytes"
    diff_ids = [] if invalid_layer_binding else [
        f"sha256:{hashlib.sha256(layer_content).hexdigest()}"
    ]
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    image_id = f"sha256:{hashlib.sha256(config).hexdigest()}"
    layer_name = f"{'a' * 64}/layer.tar"
    manifest: list[dict[str, object]] = [
        {
            "Config": f"{image_id.removeprefix('sha256:')}.json",
            "RepoTags": repo_tags,
            "Layers": [layer_name],
        }
    ]
    if extra_manifest_entry:
        manifest.append(dict(manifest[0]))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        for name, content in (
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            (f"{image_id.removeprefix('sha256:')}.json", config),
            (layer_name, layer_content),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mtime = 0
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue(), image_id


def _oci_docker_image_archive_bytes(
    *,
    masquerading_attestation: bool = False,
) -> tuple[bytes, str]:
    def encoded(value: object) -> bytes:
        return json.dumps(value, separators=(",", ":")).encode("utf-8")

    blobs: dict[str, bytes] = {}

    def add_blob(content: bytes) -> tuple[str, int]:
        digest = hashlib.sha256(content).hexdigest()
        blobs[digest] = content
        return f"sha256:{digest}", len(content)

    primary_layer = b"primary-layer"
    primary_layer_digest, primary_layer_size = add_blob(primary_layer)
    primary_config = encoded(
        {
            "architecture": "amd64",
            "os": "linux",
            "config": {},
            "rootfs": {"type": "layers", "diff_ids": [primary_layer_digest]},
        }
    )
    primary_config_digest, primary_config_size = add_blob(primary_config)
    primary_manifest = encoded(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": primary_config_digest,
                "size": primary_config_size,
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": primary_layer_digest,
                    "size": primary_layer_size,
                }
            ],
        }
    )
    primary_manifest_digest, primary_manifest_size = add_blob(primary_manifest)
    descriptors: list[dict[str, object]] = [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": primary_manifest_digest,
            "size": primary_manifest_size,
            "platform": {"architecture": "amd64", "os": "linux"},
        }
    ]
    legacy_config_digest = primary_config_digest
    legacy_layers = [primary_layer_digest]
    if masquerading_attestation:
        hidden_layer = b"second-runnable-layer"
        hidden_layer_digest, hidden_layer_size = add_blob(hidden_layer)
        hidden_config = encoded(
            {
                "architecture": "amd64",
                "os": "linux",
                "config": {"Cmd": ["run-hidden-image"]},
                "rootfs": {
                    "type": "layers",
                    "diff_ids": [hidden_layer_digest],
                },
            }
        )
        hidden_config_digest, hidden_config_size = add_blob(hidden_config)
        hidden_manifest = encoded(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": hidden_config_digest,
                    "size": hidden_config_size,
                },
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar",
                        "digest": hidden_layer_digest,
                        "size": hidden_layer_size,
                    }
                ],
            }
        )
        hidden_manifest_digest, hidden_manifest_size = add_blob(hidden_manifest)
        descriptors.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": hidden_manifest_digest,
                "size": hidden_manifest_size,
                "annotations": {
                    "vnd.docker.reference.digest": primary_manifest_digest,
                    "vnd.docker.reference.type": "attestation-manifest",
                },
                "platform": {"architecture": "unknown", "os": "unknown"},
            }
        )
        legacy_config_digest = hidden_config_digest
        legacy_layers = [hidden_layer_digest]

    root_index = encoded(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": descriptors,
        }
    )
    root_digest, root_size = add_blob(root_index)
    index = encoded(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "digest": root_digest,
                    "size": root_size,
                }
            ],
        }
    )
    legacy = encoded(
        [
            {
                "Config": (
                    f"blobs/sha256/{legacy_config_digest.removeprefix('sha256:')}"
                ),
                "RepoTags": None,
                "Layers": [
                    f"blobs/sha256/{item.removeprefix('sha256:')}"
                    for item in legacy_layers
                ],
            }
        ]
    )
    members = {
        "manifest.json": legacy,
        "index.json": index,
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        **{f"blobs/sha256/{digest}": content for digest, content in blobs.items()},
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        for name, content in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mtime = 0
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue(), root_digest


REPOSITORY_ROOT = BACKEND_ROOT.parent
GENERAL_ROOT = BACKEND_ROOT / "eval_data" / "agent_gate_v1"
G01_ROOT = BACKEND_ROOT / "eval_data" / "trip_text_cards_agent_v2"
GOAL_ID = "TC-VNEXT-G01-TEXT-CARDS"


def _git(format_value: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "show", "-s", f"--format={format_value}", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


SUBJECT = _git("%H")
TREE = _git("%T")


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _manifest_and_keys(
    tmp_path: Path,
    *,
    g01_contract_path: str = "backend/eval_data/agent_gate_v1/g01_automated_product_gate.json",
    g01_contract_sha256: str | None = None,
):
    registry = tmp_path / "custody" / "registry.sqlite3"
    registry.parent.mkdir(parents=True)
    identity = initialize_custody_registry(
        registry_path=registry,
        repository_root=REPOSITORY_ROOT,
    )
    roles = (
        "SEALED_CUSTODY",
        "AMAP_LIVE_EXPORTER",
        "QWEN_LIVE_EXPORTER",
        "AUTOMATED_PRODUCT_GATE",
        "LIVE_PROVIDER_GATE",
        "MULTI_AGENT_PANEL",
        "SEALED_AGENT_BLIND",
        "FINAL_GATE",
    )
    keys = {}
    authorities = []
    for index, role in enumerate(roles, start=1):
        key_path = tmp_path / "keys" / f"{role.lower()}.key"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = generate_authority_keypair(
            private_key_path=key_path,
            repository_root=REPOSITORY_ROOT,
            role=role,
            authority_id=f"AUTH-{role.replace('_', '-')}-{index:06d}",
            task_id=f"isolated-authority-task-{index}",
        )
        keys[role] = key_path
        authorities.append(key.model_dump(mode="json"))
    goal_ids = [
        "TC-VNEXT-G01-TEXT-CARDS",
        "TC-VNEXT-G02-MAP-STAY",
        "TC-VNEXT-G03-TOP3-AUDIT",
        "TC-VNEXT-G04-SCREENSHOT",
        "TC-VNEXT-G05-CITY-KNOWLEDGE",
        "TC-VNEXT-G06-MEMORY-SHARE",
        "TC-VNEXT-G07-CANDIDATE",
    ]
    goal_paths = [
        g01_contract_path,
        *(
            f"backend/eval_data/agent_gate_v1/g{index:02d}_automated_product_gate.json"
            for index in range(2, 8)
        ),
    ]
    goal_hashes = [
        g01_contract_sha256
        or hashlib.sha256((REPOSITORY_ROOT / g01_contract_path).read_bytes()).hexdigest(),
        *(
            hashlib.sha256((REPOSITORY_ROOT / path).read_bytes()).hexdigest()
            for path in goal_paths[1:]
        ),
    ]
    verifier_path = "backend/evals/agent_gate_v1/component_verifiers.py"
    exporter_paths = {
        "AMAP_LIVE_EXPORTER": "backend/scripts/export_g01_amap_live_receipts.py",
        "QWEN_LIVE_EXPORTER": "backend/scripts/export_g01_qwen_live_receipts.py",
    }
    manifest = AgentGateAuthorityManifest.model_validate(
        {
            "schema_version": "agent-gate-authority-manifest-v1",
            "program_id": "TC-VNEXT-2026",
            "policy_id": "TC-VNEXT-AGENT-GATE-V1",
            "scope_goal_ids": goal_ids,
            "canonical_candidate_ref": "refs/heads/codex/trip-check-product-reset",
            "candidate_freeze_ref_prefix": "refs/heads/codex/agent-gate-candidates/",
            "authority_generation": 1,
            "authority_phase": "ACTIVE",
            "canonical_origin_url": "https://github.com/Munto47/BreezeTravel.git",
            "legacy_baseline_commit": "7bdd1a6abd9c10c6076aca67f08de785027501a0",
            "custody_registry_identity_sha256": identity,
            "custody_registry_path_sha256": hashlib.sha256(
                str(registry.resolve()).lower().encode("utf-8")
            ).hexdigest(),
            "config_roots": ["backend/eval_data/agent_gate_v1/authority_policy.json"],
            "data_roots": ["backend/eval_data/trip_text_cards_v1/dataset_contract.json"],
            "program_core_paths": [
                verifier_path,
                *exporter_paths.values(),
                *goal_paths,
            ],
            "immutable_protocol_paths": [
                verifier_path,
                *exporter_paths.values(),
                *goal_paths,
            ],
            "bootstrap_core_paths": [verifier_path],
            "current_goal_binding_path": "docs/governance/current_goal_binding.json",
            "goal_bindings": [
                {
                    "goal_sequence": index,
                    "goal_id": goal_id,
                    "predecessor_goal_id": (
                        "TC-BP-G00-BLUEPRINT" if index == 1 else goal_ids[index - 2]
                    ),
                    "initial_predecessor_completion_commit": (
                        "f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac"
                        if index == 1
                        else None
                    ),
                    "automated_gate_contract_path": goal_paths[index - 1],
                    "automated_gate_contract_sha256": goal_hashes[index - 1],
                }
                for index, goal_id in enumerate(goal_ids, start=1)
            ],
            "component_verifier_paths": {
                component: verifier_path
                for component in (
                    "AUTOMATED_PRODUCT_GATE",
                    "LIVE_PROVIDER_GATE",
                    "MULTI_AGENT_PANEL",
                    "SEALED_AGENT_BLIND",
                )
            },
            "live_exporter_paths": exporter_paths,
            "authorities": authorities,
            "frozen_at": datetime(2026, 8, 28, tzinfo=UTC).isoformat(),
            "process_isolation_only": True,
            "human_evidence": False,
            "status": "GOAL_SCOPED_IMMUTABLE_GENERATION",
        }
    )
    return manifest, keys, registry


def test_generated_agent_gate_contracts_match_checked_in_bytes(tmp_path: Path) -> None:
    general = tmp_path / "general"
    g01 = tmp_path / "g01"
    canonical_binding = REPOSITORY_ROOT / "docs/governance/current_goal_binding.json"
    binding_before = canonical_binding.read_bytes()
    generate(general_root=general, g01_root=g01)
    assert canonical_binding.read_bytes() == binding_before
    for source_root, generated_root in ((GENERAL_ROOT, general), (G01_ROOT, g01)):
        generated_names = {path.name for path in generated_root.iterdir() if path.is_file()}
        for name in generated_names:
            assert (generated_root / name).read_bytes() == (
                source_root / name
            ).read_bytes(), name
    assert not (GENERAL_ROOT / "component_receipt.schema.json").exists()


def test_checked_in_authority_policy_pins_external_distinct_roles_and_real_paths() -> None:
    policy_path = GENERAL_ROOT / "authority_policy.json"
    manifest = AgentGateAuthorityManifest.model_validate_json(policy_path.read_bytes())
    assert manifest.process_isolation_only is True
    assert manifest.human_evidence is False
    assert manifest.authority_phase == "BOOTSTRAP"
    assert set(manifest.bootstrap_core_paths).issubset(manifest.program_core_paths)
    assert len(manifest.authorities) == 8
    assert len({item.role for item in manifest.authorities}) == 8
    assert len({item.public_key_sha256 for item in manifest.authorities}) == 8
    assert all(item.private_key_storage == "REPOSITORY_EXTERNAL" for item in manifest.authorities)
    assert all(item.human_evidence is False for item in manifest.authorities)
    assert "backend/eval_data/agent_gate_v1/authority_policy.json" in manifest.config_roots
    for relative in (*manifest.config_roots, *manifest.data_roots):
        assert (REPOSITORY_ROOT / relative).exists(), relative
    for relative in (
        *manifest.immutable_protocol_paths,
        *manifest.component_verifier_paths.values(),
    ):
        assert (REPOSITORY_ROOT / relative).is_file(), relative
    weakened = manifest.model_dump(mode="json")
    weakened["program_core_paths"] = weakened["program_core_paths"][:-1]
    with pytest.raises(ValueError, match="must be identical"):
        AgentGateAuthorityManifest.model_validate(weakened)


def test_bootstrap_policy_cannot_register_anchor_or_produce_gate_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import authority as authority_module

    policy = (GENERAL_ROOT / "authority_policy.json").read_bytes()
    monkeypatch.setattr(
        authority_module,
        "_git",
        lambda *_args, **_kwargs: policy,
    )
    monkeypatch.setattr(authority_module, "_is_ancestor", lambda *_args: True)
    with pytest.raises(AuthorityPolicyError, match="bootstrap cannot produce"):
        load_anchored_authority_policy(REPOSITORY_ROOT, "a" * 40)


def test_active_g01_requires_custody_signed_activation_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import authority as authority_module

    bootstrap = AgentGateAuthorityManifest.model_validate_json(
        (GENERAL_ROOT / "authority_policy.json").read_bytes()
    )
    active = bootstrap.model_copy(
        update={
            "authority_phase": "ACTIVE",
            "frozen_at": datetime(2026, 8, 29, tzinfo=UTC),
        }
    )

    def missing_receipt(*_args, **_kwargs):
        raise AuthorityPolicyError("missing activation receipt")

    monkeypatch.setattr(authority_module, "_git", missing_receipt)
    with pytest.raises(AuthorityPolicyError, match="activation readiness receipt"):
        _validate_generation_one_activation(
            root=REPOSITORY_ROOT,
            manifest=active,
            active_policy_content=active.model_dump_json().encode("utf-8"),
            anchor_commit="a" * 40,
            bootstrap_commit="b" * 40,
            bootstrap=bootstrap,
        )

    invalid = {
        "bootstrap_commit": "b" * 40,
        "bootstrap_tree": "c" * 40,
        "bootstrap_policy_sha256": "1" * 64,
        "active_policy_sha256": "2" * 64,
        "bootstrap_core_sha256": "3" * 64,
        "active_tree_without_receipt_sha256": "b" * 64,
        "active_program_core_sha256": "c" * 64,
        "active_config_sha256": "d" * 64,
        "active_data_sha256": "e" * 64,
        "live_capture": {
            "amap_execution_receipt_sha256": "4" * 64,
            "qwen_execution_receipt_sha256": "5" * 64,
            "capture_runner_sha256": "6" * 64,
            "registry_contract_sha256": "7" * 64,
            "direct_https_capture": True,
            "one_shot_mint": True,
            "complete_coverage": True,
            "status": "READY",
        },
        "external_signer": {
            "signer_bundle_sha256": "8" * 64,
            "signer_execution_receipt_sha256": "9" * 64,
            "repository_external": True,
            "imports_candidate_code": False,
            "private_key_in_candidate_process": False,
            "key_path_in_candidate_environment": False,
            "status": "READY",
        },
        "created_at": "2026-08-28T12:00:00Z",
        "process_isolation_only": True,
        "human_evidence": False,
        "status": "FORMAL_ACTIVATION_READY",
        "authority_signature": {
            "authority_role": "FINAL_GATE",
            "authority_id": "AUTH-FINAL-GATE-000001",
            "algorithm": "ED25519",
            "signed_payload_sha256": "a" * 64,
            "signature_base64": "A" * 88,
        },
    }
    with pytest.raises(ValueError, match="pinned custody authority"):
        AuthorityActivationReadinessReceipt.model_validate(invalid)


def test_valid_activation_readiness_binds_the_complete_active_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import authority as authority_module

    active, keys, _registry = _manifest_and_keys(tmp_path)
    active = active.model_copy(
        update={"frozen_at": datetime(2026, 8, 29, tzinfo=UTC)}
    )
    bootstrap = active.model_copy(
        update={
            "authority_phase": "BOOTSTRAP",
            "frozen_at": datetime(2026, 8, 27, tzinfo=UTC),
        }
    )
    bootstrap_policy = b"bootstrap-policy"
    active_policy = b"active-policy"
    registry_contract = b"typed-registry-contract"
    unsigned = {
        "schema_version": "authority-activation-readiness-v1",
        "policy_id": active.policy_id,
        "authority_generation": 1,
        "goal_id": GOAL_ID,
        "bootstrap_commit": "b" * 40,
        "bootstrap_tree": "c" * 40,
        "bootstrap_policy_sha256": hashlib.sha256(bootstrap_policy).hexdigest(),
        "active_policy_sha256": hashlib.sha256(active_policy).hexdigest(),
        "bootstrap_core_sha256": "1" * 64,
        "active_tree_without_receipt_sha256": "2" * 64,
        "active_program_core_sha256": "3" * 64,
        "active_config_sha256": "4" * 64,
        "active_data_sha256": "5" * 64,
        "live_capture": {
            "amap_execution_receipt_sha256": "6" * 64,
            "qwen_execution_receipt_sha256": "7" * 64,
            "capture_runner_sha256": "8" * 64,
            "registry_contract_sha256": hashlib.sha256(
                registry_contract
            ).hexdigest(),
            "direct_https_capture": True,
            "one_shot_mint": True,
            "complete_coverage": True,
            "status": "READY",
        },
        "external_signer": {
            "signer_bundle_sha256": "9" * 64,
            "signer_execution_receipt_sha256": "a" * 64,
            "repository_external": True,
            "imports_candidate_code": False,
            "private_key_in_candidate_process": False,
            "key_path_in_candidate_environment": False,
            "status": "READY",
        },
        "created_at": "2026-08-28T12:00:00Z",
        "process_isolation_only": True,
        "human_evidence": False,
        "status": "FORMAL_ACTIVATION_READY",
    }
    signature = sign_payload_for_development(
        payload=unsigned,
        private_key_path=keys["SEALED_CUSTODY"],
        repository_root=REPOSITORY_ROOT,
        authority=next(
            item for item in active.authorities if item.role == "SEALED_CUSTODY"
        ),
    )
    receipt = AuthorityActivationReadinessReceipt.model_validate(
        {**unsigned, "authority_signature": signature.model_dump(mode="json")}
    )

    def fake_git(_root, *args, text=True):
        subject = args[-1]
        if subject.endswith(active.authority_activation_receipt_path):
            return receipt.model_dump_json().encode("utf-8")
        if subject.endswith("authority_policy.json"):
            return bootstrap_policy
        if args[:3] == ("show", "-s", "--format=%T"):
            return "c" * 40
        if subject.endswith("live_evidence_registry_contract.sql"):
            return registry_contract
        raise AssertionError(args)

    def fake_bundle(_root, _commit, roots, *, excluded_paths=None):
        if roots == active.bootstrap_core_paths:
            return "1" * 64
        if roots == active.program_core_paths:
            return "3" * 64
        if roots == active.config_roots:
            return "4" * 64
        if roots == active.data_roots:
            assert excluded_paths == {active.authority_activation_receipt_path}
            return "5" * 64
        assert excluded_paths is None
        raise AssertionError(roots)

    monkeypatch.setattr(authority_module, "_git", fake_git)
    monkeypatch.setattr(
        authority_module,
        "compute_git_blob_bundle_hash",
        fake_bundle,
    )
    monkeypatch.setattr(
        authority_module,
        "compute_git_tree_bundle_hash_excluding",
        lambda *_args, **_kwargs: "2" * 64,
    )
    _validate_generation_one_activation(
        root=REPOSITORY_ROOT,
        manifest=active,
        active_policy_content=active_policy,
        anchor_commit="a" * 40,
        bootstrap_commit="b" * 40,
        bootstrap=bootstrap,
    )


def test_active_tree_bundle_excludes_only_the_signed_readiness_receipt(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "activation-tree"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Activation Test"],
        check=True,
    )
    receipt_path = "authority/activation.json"
    (repository / "authority").mkdir()
    (repository / receipt_path).write_text("receipt-a\n", encoding="utf-8")
    (repository / "core.txt").write_text("core-a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "active-a"],
        check=True,
    )
    first = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    first_hash = compute_git_tree_bundle_hash_excluding(
        repository,
        first,
        {receipt_path},
    )
    first_data_hash = compute_git_blob_bundle_hash(
        repository,
        first,
        ["authority", "core.txt"],
        excluded_paths={receipt_path},
    )
    (repository / receipt_path).write_text("receipt-b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "commit", "-am", "receipt"], check=True)
    second = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert compute_git_tree_bundle_hash_excluding(
        repository,
        second,
        {receipt_path},
    ) == first_hash
    assert compute_git_blob_bundle_hash(
        repository,
        second,
        ["authority", "core.txt"],
        excluded_paths={receipt_path},
    ) == first_data_hash
    (repository / "core.txt").write_text("core-b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "commit", "-am", "core"], check=True)
    third = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert compute_git_tree_bundle_hash_excluding(
        repository,
        third,
        {receipt_path},
    ) != first_hash
    assert compute_git_blob_bundle_hash(
        repository,
        third,
        ["authority", "core.txt"],
        excluded_paths={receipt_path},
    ) != first_data_hash


def test_bootstrap_formal_entrypoints_never_read_private_key_paths() -> None:
    scripts = (
        "build_agent_gate_component.py",
        "build_agent_gate_pass.py",
        "export_g01_amap_live_receipts.py",
        "export_g01_qwen_live_receipts.py",
    )
    for name in scripts:
        source = (BACKEND_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "KEY_PATH" not in source
        assert "private_key_path" not in source
        assert "repository-external" in source
        assert "NOT_RUN during BOOTSTRAP" in source

    formal_modules = (
        "component_builders.py",
        "final_gate.py",
        "live_export.py",
    )
    for name in formal_modules:
        source = (BACKEND_ROOT / "evals/agent_gate_v1" / name).read_text(
            encoding="utf-8"
        )
        assert "development_signing" not in source
        assert "private_key_path" not in source
        assert "sign_payload_for_development" not in source
    formal_callables = (
        build_automated_product_component,
        build_live_provider_component,
        build_multi_agent_panel_component,
        build_sealed_agent_blind_component,
        export_live_lane,
        verify_agent_gate_pass,
    )
    for callable_ in formal_callables:
        parameters = inspect.signature(callable_).parameters
        assert not any("private_key" in name for name in parameters)


def test_new_authority_manifest_fields_are_stable_by_default(tmp_path: Path) -> None:
    manifest, _keys, _registry = _manifest_and_keys(tmp_path)
    for field, replacement in (
        ("config_roots", [*manifest.config_roots, "new/config.json"]),
        ("data_roots", [*manifest.data_roots, "new/data.json"]),
        ("live_exporter_paths", {**manifest.live_exporter_paths, "AMAP_LIVE_EXPORTER": "new.py"}),
    ):
        changed = manifest.model_copy(update={field: replacement})
        assert _stable_program_facts(changed) != _stable_program_facts(manifest)
    allowed = manifest.model_copy(
        update={
            "authority_generation": 2,
            "frozen_at": datetime(2026, 8, 29, tzinfo=UTC),
        }
    )
    assert _stable_program_facts(allowed) == _stable_program_facts(manifest)


def test_all_goal_contracts_bind_exact_runner_assets_and_stable_program_core() -> None:
    manifest = AgentGateAuthorityManifest.model_validate_json(
        (GENERAL_ROOT / "authority_policy.json").read_bytes()
    )
    assert manifest.program_core_paths == manifest.immutable_protocol_paths
    critical_core = {
        "backend/evals/agent_gate_v1/authority.py",
        "backend/evals/agent_gate_v1/automation_isolation.py",
        "backend/evals/agent_gate_v1/component_verifiers.py",
        "backend/evals/agent_gate_v1/custody.py",
        "backend/evals/agent_gate_v1/final_gate.py",
        "backend/evals/agent_gate_v1/host_tools.py",
        "backend/evals/trip_text_cards_v1/contracts.py",
        "backend/evals/trip_text_cards_v1/scorer.py",
        "backend/eval_data/agent_gate_v1/automation_runner_requirements.lock",
        "backend/eval_data/agent_gate_v1/automation_runner_browser_package.json",
        (
            "backend/eval_data/agent_gate_v1/"
            "automation_runner_browser_package-lock.json"
        ),
    }
    assert critical_core.issubset(manifest.program_core_paths)

    asset_fields = (
        ("runner_recipe_path", "runner_recipe_sha256"),
        ("runner_entrypoint_path", "runner_entrypoint_sha256"),
        ("runner_context_policy_path", "runner_context_policy_sha256"),
    )
    for sequence in range(1, 8):
        contract = AutomatedProductGateContract.model_validate_json(
            (GENERAL_ROOT / f"g{sequence:02d}_automated_product_gate.json").read_bytes()
        )
        for path_field, hash_field in asset_fields:
            relative_path = getattr(contract.isolation, path_field)
            actual = hashlib.sha256(
                (REPOSITORY_ROOT / relative_path).read_bytes()
            ).hexdigest()
            assert getattr(contract.isolation, hash_field) == actual


def test_current_goal_document_machine_state_rejects_false_completion_or_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import authority as authority_module

    manifest = AgentGateAuthorityManifest.model_validate_json(
        (GENERAL_ROOT / "authority_policy.json").read_bytes()
    )
    binding = CurrentGoalBinding.model_validate_json(
        (REPOSITORY_ROOT / manifest.current_goal_binding_path).read_bytes()
    )
    current_goal_bytes = (
        REPOSITORY_ROOT / manifest.current_goal_document_path
    ).read_bytes()
    monkeypatch.setattr(
        authority_module,
        "_git",
        lambda *_args, **_kwargs: current_goal_bytes,
    )
    state, document_sha256 = load_current_goal_document_state(
        REPOSITORY_ROOT,
        "a" * 40,
        manifest,
        binding,
    )
    assert state.gate_result == "AGENT_GATE_NOT_RUN"
    assert state.goal_archived is False
    assert state.next_activated is False
    assert document_sha256 == hashlib.sha256(current_goal_bytes).hexdigest()

    false_activation = current_goal_bytes.replace(
        b"- Next activated\xef\xbc\x9a`NO`\xef\xbc\x9b",
        b"- Next activated\xef\xbc\x9a`YES`\xef\xbc\x9b",
        1,
    )
    assert false_activation != current_goal_bytes
    monkeypatch.setattr(
        authority_module,
        "_git",
        lambda *_args, **_kwargs: false_activation,
    )
    with pytest.raises(AuthorityPolicyError, match="visible state disagrees"):
        load_current_goal_document_state(
            REPOSITORY_ROOT,
            "a" * 40,
            manifest,
            binding,
        )

    duplicate_completion = current_goal_bytes + (
        b"\n## Completion record\n"
        b"- Status: `PENDING`;\n"
        b"- Verification / Evidence / Gate result: `AGENT_GATE_PASS`;\n"
    )
    monkeypatch.setattr(
        authority_module,
        "_git",
        lambda *_args, **_kwargs: duplicate_completion,
    )
    with pytest.raises(AuthorityPolicyError, match="no unique Completion record"):
        load_current_goal_document_state(
            REPOSITORY_ROOT,
            "a" * 40,
            manifest,
            binding,
        )

    duplicate_active_goal = current_goal_bytes + (
        b"\n# IN_PROGRESS GOAL: duplicate\n"
        b"Goal ID: TC-VNEXT-G01-TEXT-CARDS\n"
        b"Status: IN_PROGRESS\n"
    )
    # Use the governed full-width heading punctuation so the duplicate is a
    # real second visible active Goal, not an inert prose mention.
    duplicate_active_goal = duplicate_active_goal.replace(
        b"# IN_PROGRESS GOAL: duplicate",
        "# IN_PROGRESS GOAL：duplicate".encode("utf-8"),
    )
    monkeypatch.setattr(
        authority_module,
        "_git",
        lambda *_args, **_kwargs: duplicate_active_goal,
    )
    with pytest.raises(AuthorityPolicyError, match="visible state disagrees"):
        load_current_goal_document_state(
            REPOSITORY_ROOT,
            "a" * 40,
            manifest,
            binding,
        )

    false_pass = current_goal_bytes.replace(
        b"AGENT_GATE_NOT_RUN / TEXT_CARD_GATE_NOT_RUN",
        (
            b"AGENT_GATE_NOT_RUN / AGENT_GATE_PASS / LIVE_PROVIDER_PASS / "
            b"SEALED_AGENT_BLIND_PASS / TEXT_CARD_GATE_NOT_RUN"
        ),
        1,
    )
    assert false_pass != current_goal_bytes
    monkeypatch.setattr(
        authority_module,
        "_git",
        lambda *_args, **_kwargs: false_pass,
    )
    with pytest.raises(AuthorityPolicyError, match="visible state disagrees"):
        load_current_goal_document_state(
            REPOSITORY_ROOT,
            "a" * 40,
            manifest,
            binding,
        )


def test_automation_host_command_cannot_be_shadowed_by_candidate_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "docker.exe").write_bytes(b"candidate-controlled")
    authority_docker = tmp_path / "authority" / "docker.exe"
    authority_docker.parent.mkdir()
    authority_docker.write_bytes(b"authority-owned")
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(argv, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(
        isolation_module,
        "trusted_host_tool",
        lambda name: str(authority_docker.resolve()) if name == "docker" else name,
    )
    monkeypatch.setattr(isolation_module.subprocess, "run", fake_run)
    result = _run_docker(["version"], cwd=candidate, timeout=10)
    assert result.returncode == 0
    assert captured["argv"] == [str(authority_docker.resolve()), "version"]
    assert captured["cwd"] == candidate


def test_automation_not_run_manifest_cannot_be_mistaken_for_pass() -> None:
    common = {
        "goal_id": GOAL_ID,
        "candidate_commit": "1" * 40,
        "candidate_tree": "2" * 40,
        "candidate_config_sha256": "3" * 64,
        "candidate_data_sha256": "4" * 64,
        "gate_contract_sha256": "5" * 64,
        "isolation_mode": "OCI_EPHEMERAL_NO_HOST_MOUNTS",
        "runner_recipe_sha256": "6" * 64,
        "runner_entrypoint_sha256": "7" * 64,
        "runner_context_policy_sha256": "8" * 64,
        "network_access": False,
        "host_mount_count": 0,
        "host_pid_namespace": False,
        "synthetic_profile": True,
        "authority_secret_mount_count": 0,
    }
    not_run = AutomatedProductExecutionManifest.model_validate(
        {
            **common,
            "runner_image_id": None,
            "checks": [],
            "checks_not_run": ["backend.current_suite"],
            "failure_stage": "OCI_RUNNER_UNAVAILABLE",
            "verdict": "NOT_RUN",
        }
    )
    assert not_run.verdict == "NOT_RUN"
    with pytest.raises(ValueError, match="complete OCI run"):
        AutomatedProductExecutionManifest.model_validate(
            {
                **not_run.model_dump(mode="json"),
                "verdict": "PASS",
                "failure_stage": None,
                "checks_not_run": [],
            }
        )


def test_live_component_builder_and_verifier_are_fail_closed_until_capture_chain() -> None:
    with pytest.raises(ComponentBuildError, match="direct-HTTPS capture"):
        build_live_provider_component(
            repository_root=REPOSITORY_ROOT,
            amap_provider_index_path=Path("missing-amap-index.json"),
            amap_runtime_path=Path("missing-amap-runtime.json"),
            qwen_runtime_path=Path("missing-qwen-runtime.json"),
            verification_output=Path("missing-live-verification.json"),
            component_output=Path("missing-live-component.json"),
            authority_signature=DetachedAuthoritySignature.model_validate(
                {
                    "authority_role": "LIVE_PROVIDER_GATE",
                    "authority_id": "AUTH-LIVE-PROVIDER-GATE-000001",
                    "signed_payload_sha256": "0" * 64,
                    "signature_base64": "A" * 88,
                }
            ),
        )
    verifier_source = (
        BACKEND_ROOT / "evals/agent_gate_v1/component_verifiers.py"
    ).read_text(encoding="utf-8")
    branch = verifier_source.split(
        "if isinstance(receipt, LiveProviderGateReceipt):",
        maxsplit=1,
    )[1].split("if isinstance(receipt, MultiAgentPanelGateReceipt):", maxsplit=1)[0]
    assert "raise ComponentVerificationError(" in branch
    assert "direct-HTTPS capture execution receipt" in branch


def test_sealed_tranche_commitment_is_domain_structured_and_scorer_has_no_metric_input() -> None:
    commitment = tranche_commitment_sha256(
        input_bundle_sha256="1" * 64,
        case_set_commitment_sha256="2" * 64,
        truth_bundle_commitment={
            "algorithm": "HMAC-SHA256",
            "key_id": "CUSTODY-TEST-000001",
            "value": "3" * 64,
        },
    )
    changed = tranche_commitment_sha256(
        input_bundle_sha256="1" * 64,
        case_set_commitment_sha256="2" * 64,
        truth_bundle_commitment={
            "algorithm": "HMAC-SHA256",
            "key_id": "CUSTODY-TEST-000001",
            "value": "4" * 64,
        },
    )
    assert commitment != changed
    source = (BACKEND_ROOT / "scripts" / "score_g01_sealed_agent_blind.py").read_text(
        encoding="utf-8"
    )
    assert 'parser.add_argument("--truth"' in source
    assert 'parser.add_argument("--predictions"' in source
    assert 'parser.add_argument("--inputs"' in source
    assert 'parser.add_argument("--metrics"' not in source


def test_review_schema_cannot_report_human_evidence_or_pass_with_p1() -> None:
    schema = (GENERAL_ROOT / "review.schema.json").read_text(encoding="utf-8")
    assert "human_label" not in schema
    assert "is_authorized_human" not in schema
    value = {
        "schema_version": "agent-gate-review-v1",
        "goal_id": GOAL_ID,
        "candidate_commit": SUBJECT,
        "candidate_tree": TREE,
        "candidate_config_sha256": "1" * 64,
        "candidate_data_sha256": "2" * 64,
        "reviewer_role": "PRODUCT_UX",
        "attestation": {
            "task_id": "isolated-review-task",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "prompt_sha256": "3" * 64,
            "input_bundle_sha256": "4" * 64,
            "output_schema_sha256": "5" * 64,
            "subject_commit": SUBJECT,
            "subject_tree": TREE,
            "started_at": "2026-08-28T00:00:00Z",
            "completed_at": "2026-08-28T00:01:00Z",
            "frozen_at": "2026-08-28T00:02:00Z",
            "context_fork": "none",
            "isolated_context": True,
            "human_evidence": False,
            "saw_prior_verdict": False,
        },
        "scenario_coverage": {
            name: {"status": "PASS", "evidence": [{
                "kind": "TEST",
                "artifact_path": "evidence.json",
                "storage": "REPOSITORY",
                "sha256": "6" * 64,
            }]}
            for name in (
                "normal", "ambiguous", "boundary", "adversarial",
                "provider_failure", "privacy", "concurrency",
            )
        },
        "findings": [{
            "finding_id": "AGF-P1-BLOCK",
            "severity": "P1",
            "category": "blocking finding",
            "expected": "Expected behavior remains safe.",
            "observed": "Observed behavior violates the contract.",
            "reproduction_steps": ["Run the failing scenario."],
            "evidence": [{
                "kind": "TEST",
                "artifact_path": "evidence.json",
                "storage": "REPOSITORY",
                "sha256": "6" * 64,
            }],
        }],
        "checks_not_run": [],
        "evidence_level": "MULTI_AGENT_SIMULATED_REVIEW",
        "verdict": "PASS",
    }
    with pytest.raises(ValueError, match="P0/P1"):
        AgentGateReviewReceipt.model_validate(value)


def test_external_paths_reject_repository_targets_existing_targets_and_hardlinks(
    tmp_path: Path,
) -> None:
    with pytest.raises(ArtifactPathError, match="outside"):
        require_external_target(REPOSITORY_ROOT / "forbidden.json", REPOSITORY_ROOT)
    existing = tmp_path / "existing.json"
    existing.write_text("x", encoding="utf-8")
    with pytest.raises(ArtifactPathError, match="already exists"):
        require_external_target(existing, REPOSITORY_ROOT)
    link = tmp_path / "hardlink.json"
    try:
        link.hardlink_to(existing)
    except OSError:
        pytest.skip("filesystem does not support hardlink test")
    with pytest.raises(ArtifactPathError, match="hard link"):
        require_external_existing(link, REPOSITORY_ROOT)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle deletion contract")
def test_failed_windows_external_publish_removes_partial_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import path_security as path_security_module

    output = tmp_path / "partial-output.bin"
    with tempfile.TemporaryFile(mode="w+b") as source:
        source.write(b"must-not-survive-fsync-failure")
        source.seek(0)

        def fail_fsync(_descriptor: int) -> None:
            raise OSError("forced fsync failure")

        monkeypatch.setattr(path_security_module.os, "fsync", fail_fsync)
        with pytest.raises(OSError, match="forced fsync failure"):
            publish_external_stream_exclusive(output, source, REPOSITORY_ROOT)
    assert not output.exists()


def test_docker_client_environment_receives_no_gate_or_provider_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "BREEZETRAVEL_FINAL_GATE_KEY_PATH": "final.key",
        "BREEZETRAVEL_CUSTODY_REGISTRY_PATH": "custody.sqlite3",
        "BREEZETRAVEL_QWEN_LIVE_EXPORTER_KEY_PATH": "qwen.key",
        "BREEZETRAVEL_AMAP_LIVE_EXPORTER_KEY_PATH": "amap.key",
        "DASHSCOPE_API_KEY": "qwen-secret",
        "AMAP_API_KEY": "amap-secret",
        "PYTHONPATH": "injected-python",
        "NODE_OPTIONS": "--require injected.js",
        "NPM_CONFIG_USERCONFIG": "private-npmrc",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "safe-path")
    observed = _docker_environment()
    assert observed["PATH"] == "safe-path"
    assert set(secrets).isdisjoint(observed)
    assert "USERPROFILE" not in observed
    assert "APPDATA" not in observed
    assert "LOCALAPPDATA" not in observed


def test_external_artifacts_reject_any_git_managed_directory(tmp_path: Path) -> None:
    managed = tmp_path / "unrelated-repository"
    managed.mkdir()
    subprocess.run(["git", "init", str(managed)], check=True, capture_output=True)
    existing = managed / "artifact.json"
    existing.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ArtifactPathError, match="Git worktree"):
        require_external_existing(existing, REPOSITORY_ROOT)
    with pytest.raises(ArtifactPathError, match="Git worktree"):
        require_external_target(managed / "new-artifact.json", REPOSITORY_ROOT)

    listed = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    linked = [
        Path(line.removeprefix("worktree ")).resolve()
        for line in listed
        if line.startswith("worktree ")
        and Path(line.removeprefix("worktree ")).resolve() != REPOSITORY_ROOT.resolve()
    ]
    if linked:
        with pytest.raises(ArtifactPathError, match="linked worktree|Git worktree"):
            require_external_existing(linked[0] / "AGENTS.md", REPOSITORY_ROOT)

    bare = tmp_path / "bare-repository.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    with pytest.raises(ArtifactPathError, match="Git worktree or Git directory"):
        require_external_existing(bare / "HEAD", REPOSITORY_ROOT)


def test_live_and_controlled_database_receipts_cannot_swap_source_registry() -> None:
    common = {
        "goal_id": GOAL_ID,
        "candidate_commit": SUBJECT,
        "candidate_tree": TREE,
        "provider_binding_sha256": "1" * 64,
        "query_sha256": "2" * 64,
        "transaction_snapshot_sha256": "3" * 64,
        "exported_at": "2026-08-28T00:00:02Z",
    }
    amap_effect = {
        "effect_id": "amap-effect-0001",
        "effect_key_sha256": "4" * 64,
        "provider_binding_sha256": "1" * 64,
        "request_sha256": "5" * 64,
        "response_sha256": "6" * 64,
        "resolution_status": "UNRESOLVED",
        "started_at": "2026-08-28T00:00:00Z",
        "completed_at": "2026-08-28T00:00:01Z",
        "persisted_status": "SUCCEEDED",
    }
    with pytest.raises(ValueError, match="controlled Provider"):
        ProviderDatabaseExportReceipt.model_validate(
            {
                **common,
                "schema_version": "g01-amap-database-export-receipt-v2",
                "execution_mode": "CONTROLLED_FIXTURE",
                "source_registry": "POSTGRESQL_PROVIDER_EFFECT_REGISTRY",
                "effects": [amap_effect],
            }
        )
    qwen_effect = {
        "effect_id": "qwen-effect-0001",
        "case_id": "G01-TC-001",
        "request_sha256": "5" * 64,
        "response_sha256": "6" * 64,
        "output_sha256": "7" * 64,
        "started_at": "2026-08-28T00:00:00Z",
        "completed_at": "2026-08-28T00:00:01Z",
        "persisted_status": "SUCCEEDED",
    }
    with pytest.raises(ValueError, match="controlled inference"):
        InferenceDatabaseExportReceipt.model_validate(
            {
                **common,
                "schema_version": "g01-qwen-database-export-receipt-v1",
                "model_binding_sha256": "8" * 64,
                "execution_mode": "CONTROLLED_FIXTURE",
                "source_registry": "POSTGRESQL_INFERENCE_EFFECT_REGISTRY",
                "effects": [qwen_effect],
            }
        )


def test_live_export_is_fixed_query_only_and_candidate_provider_bound(tmp_path: Path) -> None:
    forbidden_parameters = {
        "query",
        "table",
        "payload",
        "receipt_json",
        "provider_binding",
        "database_url",
    }
    assert forbidden_parameters.isdisjoint(inspect.signature(export_live_lane).parameters)
    policy = AgentGateAuthorityManifest.model_validate_json(
        (GENERAL_ROOT / "authority_policy.json").read_bytes()
    )
    assert policy.live_exporter_paths == {
        "AMAP_LIVE_EXPORTER": "backend/scripts/export_g01_amap_live_receipts.py",
        "QWEN_LIVE_EXPORTER": "backend/scripts/export_g01_qwen_live_receipts.py",
    }
    assert "trip_g01_amap_provider_effects" in AMAP_EFFECT_QUERY
    assert "trip_g01_qwen_inference_effects" in QWEN_EFFECT_QUERY
    for query in (AMAP_EFFECT_QUERY, QWEN_EFFECT_QUERY):
        normalized = query.lower()
        assert "artifact_payload_json" not in normalized
        assert "receipt_json" not in normalized
        assert "trip_g01_agent_gate_live_artifacts" not in normalized
    registry_contract = (
        GENERAL_ROOT / "live_evidence_registry_contract.sql"
    ).read_text(encoding="utf-8")
    assert "trip_g01_amap_provider_effects" in registry_contract
    assert "trip_g01_qwen_inference_effects" in registry_contract
    assert "artifact_payload_json" not in registry_contract

    with pytest.raises(LiveEvidenceExportError, match="formal live export is NOT_RUN"):
        export_live_lane(
            lane="AMAP",
            evidence_run_id="G01-LIVE-FAILCLOSED1",
            candidate_commit=SUBJECT,
            output_dir=tmp_path,
            repository_root=REPOSITORY_ROOT,
            custody_registry_path=tmp_path / "missing-custody.sqlite3",
            registry_anchor_receipt_path=tmp_path / "missing-anchor.json",
            live_run_mint_path=tmp_path / "missing-mint.json",
        )

    repository = tmp_path / "binding-candidate"
    binding_path = repository / "backend/eval_data/trip_text_cards_agent_v2/provider_binding.json"
    _write(
        binding_path,
        {
            "status": "FROZEN",
            "amap_place_search": "AMAP_PLACE_V5",
            "amap_walking": "AMAP_WALKING_V3",
            "amap_transit": "AMAP_TRANSIT_V3",
            "qwen": {
                "exact_model_id": "qwen-plus-fixed",
                "region": "cn-beijing",
                "endpoint_sha256": "1" * 64,
            },
        },
    )
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Binding Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "freeze binding"],
        check=True,
        capture_output=True,
    )
    candidate = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    expected = hashlib.sha256(binding_path.read_bytes()).hexdigest()
    assert _require_frozen_provider_binding(
        repository,
        candidate,
        lane="QWEN",
        exact_model_id="qwen-plus-fixed",
        region="cn-beijing",
        endpoint_sha256="1" * 64,
    ) == expected
    with pytest.raises(AgentAnnotationValidationError, match="Qwen Provider binding"):
        _require_frozen_provider_binding(
            repository,
            candidate,
            lane="QWEN",
            exact_model_id="qwen-max-wrong",
            region="cn-beijing",
            endpoint_sha256="1" * 64,
        )


def test_custody_is_policy_pinned_signed_atomic_and_one_shot(tmp_path: Path) -> None:
    manifest, keys, registry = _manifest_and_keys(tmp_path)
    policy_sha = "a" * 64
    mint_path = tmp_path / "mint.json"
    values = {
        "goal_id": GOAL_ID,
        "candidate_commit": SUBJECT,
        "candidate_tree": TREE,
        "tranche_commitment_sha256": "b" * 64,
        "one_shot_nonce_sha256": "c" * 64,
        "custodian_task_id": "isolated-sealed-custodian",
        "prompt_sha256": "d" * 64,
        "schema_sha256": "e" * 64,
        "thresholds_sha256": "f" * 64,
        "config_sha256": "1" * 64,
        "provider_binding_sha256": "2" * 64,
        "scorer_sha256": "3" * 64,
    }
    unsigned_mint = {
        "schema_version": "sealed-agent-blind-mint-receipt-v2",
        "custody_registry_identity_sha256": (
            manifest.custody_registry_identity_sha256
        ),
        "custody_registry_path_sha256": manifest.custody_registry_path_sha256,
        "authority_policy_sha256": policy_sha,
        "mint_sequence": 1,
        **values,
        "state": "MINTED",
        "minted_at": "2026-08-28T00:00:00Z",
    }
    mint_signature = sign_payload_for_development(
        payload=unsigned_mint,
        private_key_path=keys["SEALED_CUSTODY"],
        repository_root=REPOSITORY_ROOT,
        authority=next(
            item for item in manifest.authorities if item.role == "SEALED_CUSTODY"
        ),
    )
    signed_mint = SealedAgentBlindMintReceipt.model_validate(
        {
            **unsigned_mint,
            "authority_signature": mint_signature.model_dump(mode="json"),
        }
    )
    result = mint_sealed_blind_run(
        registry_path=registry,
        mint_receipt_path=mint_path,
        repository_root=REPOSITORY_ROOT,
        manifest=manifest,
        authority_policy_sha256=policy_sha,
        receipt=signed_mint,
    )
    mint = json.loads(mint_path.read_text(encoding="utf-8"))
    verify_payload_signature(
        payload=unsigned_payload(mint),
        signature=DetachedAuthoritySignature.model_validate(mint["authority_signature"]),
        manifest=manifest,
        expected_role="SEALED_CUSTODY",
    )
    assert result["mint_receipt_sha256"] == hashlib.sha256(mint_path.read_bytes()).hexdigest()

    consumed = consume_minted_run(
        registry_path=registry,
        mint_receipt_path=mint_path,
        attempt_commitment_sha256="7" * 64,
        repository_root=REPOSITORY_ROOT,
        manifest=manifest,
        authority_policy_sha256=policy_sha,
    )
    assert consumed.attempt_commitment_sha256 == "7" * 64
    assert read_run_state(
        registry_path=registry,
        repository_root=REPOSITORY_ROOT,
        manifest=manifest,
        one_shot_nonce_sha256="c" * 64,
    )["state"] == "CONSUMED"
    with pytest.raises(SealedBlindCustodyError, match="already consumed"):
        consume_minted_run(
            registry_path=registry,
            mint_receipt_path=mint_path,
            attempt_commitment_sha256="8" * 64,
            repository_root=REPOSITORY_ROOT,
            manifest=manifest,
            authority_policy_sha256=policy_sha,
        )

    second_registry = tmp_path / "other" / "registry.sqlite3"
    second_registry.parent.mkdir(parents=True)
    initialize_custody_registry(
        registry_path=second_registry,
        repository_root=REPOSITORY_ROOT,
    )
    with pytest.raises(SealedBlindCustodyError, match="not pinned"):
        mint_sealed_blind_run(
            registry_path=second_registry,
            mint_receipt_path=tmp_path / "second-mint.json",
            repository_root=REPOSITORY_ROOT,
            manifest=manifest,
            authority_policy_sha256=policy_sha,
            receipt=signed_mint,
        )
    moved_registry = registry.with_name("registry-moved.sqlite3")
    registry.rename(moved_registry)
    moved_registry.rename(registry)


def test_custody_registry_busy_is_normalized_and_handles_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import custody as custody_module

    registry = tmp_path / "custody" / "registry.sqlite3"
    registry.parent.mkdir(parents=True)
    initialize_custody_registry(
        registry_path=registry,
        repository_root=REPOSITORY_ROOT,
    )
    blocker = sqlite3.connect(registry)
    blocker.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(custody_module, "SQLITE_BUSY_TIMEOUT_MS", 25)
    contender = custody_module._connect_registry(registry)
    try:
        with pytest.raises(SealedBlindCustodyError, match="busy; retry"):
            custody_module._begin_immediate(contender)
    finally:
        contender.close()
        blocker.rollback()
        blocker.close()

    setup = sqlite3.connect(registry)
    setup.execute("CREATE TABLE IF NOT EXISTS busy_probe (value INTEGER NOT NULL)")
    setup.commit()
    setup.close()
    reader = sqlite3.connect(registry)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM busy_probe").fetchone()
    try:
        with pytest.raises(SealedBlindCustodyError, match="busy; retry"):
            with custody_module._registry_transaction(
                registry,
                immediate=True,
            ) as writer:
                writer.execute("INSERT INTO busy_probe (value) VALUES (1)")
    finally:
        reader.rollback()
        reader.close()
    moved_registry = registry.with_name("registry-after-lock.sqlite3")
    registry.rename(moved_registry)
    moved_registry.rename(registry)


def test_strict_component_requires_role_signature_and_exact_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component_repo = tmp_path / "candidate-repo"
    verifier_path = "backend/evals/agent_gate_v1/component_verifiers.py"
    candidate_verifier = component_repo / verifier_path
    candidate_verifier.parent.mkdir(parents=True)
    candidate_verifier.write_bytes((REPOSITORY_ROOT / verifier_path).read_bytes())
    runner_recipe_path = "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile"
    runner_entrypoint_path = (
        "backend/eval_data/agent_gate_v1/automation_runner_entrypoint.sh"
    )
    runner_context_policy_path = (
        "backend/eval_data/agent_gate_v1/automation_runner.Dockerfile.dockerignore"
    )
    runner_assets = {
        runner_recipe_path: b"FROM scratch\n",
        runner_entrypoint_path: b"#!/usr/bin/env bash\nexec \"$@\"\n",
        runner_context_policy_path: b".git\n",
    }
    for relative_path, content in runner_assets.items():
        target = component_repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    runner_hashes = {
        relative_path: hashlib.sha256(content).hexdigest()
        for relative_path, content in runner_assets.items()
    }
    gate_contract_path = "backend/eval_data/agent_gate_v1/test_automated_gate.json"
    gate_contract_file = component_repo / gate_contract_path
    _write(
        gate_contract_file,
        {
            "schema_version": "automated-product-gate-contract-v1",
            "goal_id": GOAL_ID,
            "isolation": {
                "mode": "OCI_EPHEMERAL_NO_HOST_MOUNTS",
                "runner_recipe_path": runner_recipe_path,
                "runner_recipe_sha256": runner_hashes[runner_recipe_path],
                "runner_entrypoint_path": runner_entrypoint_path,
                "runner_entrypoint_sha256": runner_hashes[runner_entrypoint_path],
                "runner_context_policy_path": runner_context_policy_path,
                "runner_context_policy_sha256": runner_hashes[
                    runner_context_policy_path
                ],
                "network_access": False,
                "host_mount_count": 0,
                "host_pid_namespace": False,
                "synthetic_profile": True,
                "authority_secret_mount_count": 0,
            },
            "checks": [
                {
                    "check_id": "protocol.smoke",
                    "argv": ["python", "-m", "pytest", "-q", "tests/test_smoke.py"],
                    "workdir": "backend",
                    "timeout_seconds": 60,
                }
            ],
        },
    )
    _write(
        component_repo / "docs/governance/current_goal_binding.json",
        {
            "schema_version": "current-goal-binding-v1",
            "program_id": "TC-VNEXT-2026",
            "goal_sequence": 1,
            "goal_id": GOAL_ID,
            "status": "IN_PROGRESS",
            "canonical_candidate_ref": "refs/heads/codex/trip-check-product-reset",
            "predecessor_goal_id": "TC-BP-G00-BLUEPRINT",
            "predecessor_completion_commit": "f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac",
            "automated_gate_contract_path": gate_contract_path,
            "automated_gate_contract_sha256": hashlib.sha256(
                gate_contract_file.read_bytes()
            ).hexdigest(),
        },
    )
    gate_contract_sha256 = hashlib.sha256(gate_contract_file.read_bytes()).hexdigest()
    manifest, keys, _registry = _manifest_and_keys(
        tmp_path,
        g01_contract_path=gate_contract_path,
        g01_contract_sha256=gate_contract_sha256,
    )
    smoke_test = component_repo / "backend/tests/test_smoke.py"
    smoke_test.parent.mkdir(parents=True, exist_ok=True)
    smoke_test.write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "init", str(component_repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(component_repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(component_repo), "config", "user.name", "Agent Gate Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(component_repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(component_repo), "commit", "-m", "candidate"],
        check=True,
        capture_output=True,
    )
    component_subject = subprocess.run(
        ["git", "-C", str(component_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    component_tree = subprocess.run(
        ["git", "-C", str(component_repo), "show", "-s", "--format=%T", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    execution_manifest_path = tmp_path / "automated-execution.json"
    runner_image_id = f"sha256:{'8' * 64}"
    runner_image_archive_path = tmp_path / "automated-runner-image.tar"
    runner_image_archive_path.write_bytes(b"signed runner image archive")
    runner_image_archive_sha256 = hashlib.sha256(
        runner_image_archive_path.read_bytes()
    ).hexdigest()
    runner_image_archive_size = runner_image_archive_path.stat().st_size
    argv = ["python", "-m", "pytest", "-q", "tests/test_smoke.py"]
    argv_sha256 = hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    _write(
        execution_manifest_path,
        {
            "schema_version": "automated-product-execution-manifest-v1",
            "goal_id": GOAL_ID,
            "candidate_commit": component_subject,
            "candidate_tree": component_tree,
            "candidate_config_sha256": "1" * 64,
            "candidate_data_sha256": "2" * 64,
            "gate_contract_sha256": gate_contract_sha256,
            "isolation_mode": "OCI_EPHEMERAL_NO_HOST_MOUNTS",
            "runner_recipe_sha256": runner_hashes[runner_recipe_path],
            "runner_entrypoint_sha256": runner_hashes[runner_entrypoint_path],
            "runner_context_policy_sha256": runner_hashes[
                runner_context_policy_path
            ],
            "runner_image_id": runner_image_id,
            "runner_image_archive_format": "DOCKER_IMAGE_ARCHIVE_V1",
            "runner_image_archive_path": str(runner_image_archive_path),
            "runner_image_archive_sha256": runner_image_archive_sha256,
            "runner_image_archive_size": runner_image_archive_size,
            "network_access": False,
            "host_mount_count": 0,
            "host_pid_namespace": False,
            "synthetic_profile": True,
            "authority_secret_mount_count": 0,
            "checks": [
                {
                    "check_id": "protocol.smoke",
                    "argv_sha256": argv_sha256,
                    "workdir": "backend",
                    "exit_code": 0,
                    "stdout_sha256": hashlib.sha256(b"1 passed").hexdigest(),
                    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                    "started_at": "2026-08-28T00:00:00Z",
                    "completed_at": "2026-08-28T00:00:01Z",
                    "verdict": "PASS",
                }
            ],
            "checks_not_run": [],
            "verdict": "PASS",
        },
    )
    upstream_path = tmp_path / "automated-upstream.json"
    upstream = {
        "schema_version": "automated-product-verification-receipt-v2",
        "goal_id": GOAL_ID,
        "candidate_commit": component_subject,
        "candidate_tree": component_tree,
        "candidate_config_sha256": "1" * 64,
        "candidate_data_sha256": "2" * 64,
        "gate_contract_path": gate_contract_path,
        "gate_contract_sha256": gate_contract_sha256,
        "isolation_mode": "OCI_EPHEMERAL_NO_HOST_MOUNTS",
        "runner_recipe_sha256": runner_hashes[runner_recipe_path],
        "runner_entrypoint_sha256": runner_hashes[runner_entrypoint_path],
        "runner_context_policy_sha256": runner_hashes[runner_context_policy_path],
        "runner_image_id": runner_image_id,
        "runner_image_archive_format": "DOCKER_IMAGE_ARCHIVE_V1",
        "runner_image_archive_path": str(runner_image_archive_path),
        "runner_image_archive_sha256": runner_image_archive_sha256,
        "runner_image_archive_size": runner_image_archive_size,
        "network_access": False,
        "host_mount_count": 0,
        "host_pid_namespace": False,
        "execution_manifest_path": str(execution_manifest_path),
        "execution_manifest_sha256": hashlib.sha256(
            execution_manifest_path.read_bytes()
        ).hexdigest(),
        "executed_check_count": 1,
        "failed_check_count": 0,
        "checks_not_run": [],
        "verdict": "PASS",
    }
    _write(upstream_path, upstream)
    verifier_bytes = candidate_verifier.read_bytes()
    unsigned = {
        "schema_version": "automated-product-gate-receipt-v2",
        "goal_id": GOAL_ID,
        "candidate_commit": component_subject,
        "candidate_tree": component_tree,
        "candidate_config_sha256": "1" * 64,
        "candidate_data_sha256": "2" * 64,
        "component": "AUTOMATED_PRODUCT_GATE",
        "authority_policy_sha256": "5" * 64,
        "verifier_path": verifier_path,
        "verifier_sha256": hashlib.sha256(verifier_bytes).hexdigest(),
        "verification_receipt_path": str(upstream_path),
        "verification_receipt_sha256": hashlib.sha256(upstream_path.read_bytes()).hexdigest(),
        "gate_contract_path": gate_contract_path,
        "gate_contract_sha256": gate_contract_sha256,
        "execution_manifest_path": str(execution_manifest_path),
        "execution_manifest_sha256": hashlib.sha256(
            execution_manifest_path.read_bytes()
        ).hexdigest(),
        "isolation_mode": "OCI_EPHEMERAL_NO_HOST_MOUNTS",
        "runner_recipe_sha256": runner_hashes[runner_recipe_path],
        "runner_entrypoint_sha256": runner_hashes[runner_entrypoint_path],
        "runner_context_policy_sha256": runner_hashes[runner_context_policy_path],
        "runner_image_id": runner_image_id,
        "runner_image_archive_format": "DOCKER_IMAGE_ARCHIVE_V1",
        "runner_image_archive_path": str(runner_image_archive_path),
        "runner_image_archive_sha256": runner_image_archive_sha256,
        "runner_image_archive_size": runner_image_archive_size,
        "network_access": False,
        "host_mount_count": 0,
        "host_pid_namespace": False,
        "executed_check_count": 1,
        "failed_check_count": 0,
        "checks_not_run": [],
        "evidence_level": "AUTOMATED_TEST",
        "human_evidence": False,
        "verdict": "PASS",
        "completed_at": "2026-08-28T00:00:00Z",
    }
    signature = sign_payload_for_development(
        payload=unsigned,
        private_key_path=keys["AUTOMATED_PRODUCT_GATE"],
        repository_root=REPOSITORY_ROOT,
        authority=next(
            item for item in manifest.authorities if item.role == "AUTOMATED_PRODUCT_GATE"
        ),
    )
    receipt = AutomatedProductGateReceipt.model_validate(
        {**unsigned, "authority_signature": signature.model_dump(mode="json")}
    )
    anchored = AnchoredAuthorityPolicy(
        manifest=manifest,
        content=b"test-policy",
        sha256="5" * 64,
        anchor_commit=SUBJECT,
        candidate_commit=SUBJECT,
    )
    current_binding = CurrentGoalBinding.model_validate_json(
        (component_repo / "docs/governance/current_goal_binding.json").read_bytes()
    )
    monkeypatch.setattr(
        "evals.agent_gate_v1.component_verifiers.load_current_goal_binding",
        lambda *_args, **_kwargs: current_binding,
    )
    monkeypatch.setattr(
        "evals.agent_gate_v1.component_verifiers.run_isolated_check",
        lambda **_kwargs: SimpleNamespace(exit_code=0),
    )
    monkeypatch.setattr(
        "evals.agent_gate_v1.component_verifiers.ensure_isolated_candidate_image",
        lambda **_kwargs: f"breezetravel-agent-gate:{component_tree[:20]}",
    )
    verify_strict_component_receipt(
        receipt=receipt,
        repository_root=component_repo,
        anchored_policy=anchored,
        expected_goal_id=GOAL_ID,
        expected_candidate_commit=component_subject,
        expected_candidate_tree=component_tree,
        expected_config_sha256="1" * 64,
        expected_data_sha256="2" * 64,
    )
    tampered = receipt.model_copy(update={"executed_check_count": 2})
    with pytest.raises(ValueError, match="signed payload"):
        verify_strict_component_receipt(
            receipt=tampered,
            repository_root=component_repo,
            anchored_policy=anchored,
            expected_goal_id=GOAL_ID,
            expected_candidate_commit=component_subject,
            expected_candidate_tree=component_tree,
            expected_config_sha256="1" * 64,
            expected_data_sha256="2" * 64,
        )

    forged_upstream_path = tmp_path / "forged-automated-upstream.json"
    _write(
        forged_upstream_path,
        {**upstream, "executed_check_count": 2},
    )
    forged_unsigned = {
        **unsigned,
        "verification_receipt_path": str(forged_upstream_path),
        "verification_receipt_sha256": hashlib.sha256(
            forged_upstream_path.read_bytes()
        ).hexdigest(),
        "executed_check_count": 2,
    }
    forged_signature = sign_payload_for_development(
        payload=forged_unsigned,
        private_key_path=keys["AUTOMATED_PRODUCT_GATE"],
        repository_root=REPOSITORY_ROOT,
        authority=next(
            item
            for item in manifest.authorities
            if item.role == "AUTOMATED_PRODUCT_GATE"
        ),
    )
    forged_receipt = AutomatedProductGateReceipt.model_validate(
        {
            **forged_unsigned,
            "authority_signature": forged_signature.model_dump(mode="json"),
        }
    )
    with pytest.raises(ComponentVerificationError, match="raw execution"):
        verify_strict_component_receipt(
            receipt=forged_receipt,
            repository_root=component_repo,
            anchored_policy=anchored,
            expected_goal_id=GOAL_ID,
            expected_candidate_commit=component_subject,
            expected_candidate_tree=component_tree,
            expected_config_sha256="1" * 64,
            expected_data_sha256="2" * 64,
        )

    historical = tmp_path / "generic.json"
    _write(historical, {"schema_version": "agent-gate-component-receipt-v1"})
    with pytest.raises(ComponentVerificationError, match="historical"):
        load_strict_component_receipt(historical, REPOSITORY_ROOT)


def test_sealed_score_taxonomy_cannot_contradict_metrics() -> None:
    thresholds = json.loads(
        (G01_ROOT / "sealed_blind_thresholds.json").read_text(encoding="utf-8")
    )
    metrics = {name: 0 for name in thresholds["required_metric_names"]}
    for condition in thresholds["conditions"]:
        metrics[condition["metric"]] = condition["value"]
    taxonomy = {
        name: 0
        for name in (
            "WRONG_CITY", "WRONG_CATEGORY", "NON_ATOMIC_PLACE",
            "MENTION_FALSE_POSITIVE", "MENTION_FALSE_NEGATIVE",
            "DAY_ASSIGNMENT", "ROLE_CLASSIFICATION", "PROVIDER_RESOLUTION",
            "PUBLIC_LEAK", "LATENCY", "OTHER_AGGREGATED",
        )
    }
    value = {
        "schema_version": "sealed-agent-blind-score-receipt-v2",
        "goal_id": GOAL_ID,
        "candidate_commit": SUBJECT,
        "candidate_tree": TREE,
        "tranche_commitment_sha256": "1" * 64,
        "thresholds_sha256": "2" * 64,
        "scorer_sha256": "3" * 64,
        "score_input_manifest_sha256": "4" * 64,
        "input_bundle_sha256": "5" * 64,
        "prediction_bundle_sha256": "6" * 64,
        "truth_bundle_commitment": {
            "algorithm": "HMAC-SHA256",
            "key_id": "CUSTODY-TEST-000001",
            "value": "7" * 64,
        },
        "case_set_commitment_sha256": "8" * 64,
        "scored_case_count": 18,
        "custody_registry_identity_sha256": "9" * 64,
        "mint_receipt_sha256": "a" * 64,
        "one_shot_nonce_sha256": "b" * 64,
        "attempt_commitment_sha256": "e" * 64,
        "authority_policy_sha256": "c" * 64,
        "generated_by": "SEALED_AGENT_BLIND_SCORER",
        "aggregate_metrics": metrics,
        "taxonomy_counts": taxonomy,
        "unmapped_error_count": 0,
        "required_gate_metrics_passed": True,
        "raw_truth_in_receipt": False,
        "completed_at": "2026-08-28T00:00:00Z",
        "authority_signature": {
            "authority_role": "SEALED_CUSTODY",
            "authority_id": "AUTH-SEALED-CUSTODY-000001",
            "algorithm": "ED25519",
            "signed_payload_sha256": "d" * 64,
            "signature_base64": "A" * 88,
        },
    }
    SealedAgentBlindScoreReceipt.model_validate(value)
    value["taxonomy_counts"]["WRONG_CITY"] = 1
    with pytest.raises(ValueError, match="contradicts"):
        SealedAgentBlindScoreReceipt.model_validate(value)


def test_component_builders_accept_raw_artifacts_not_summary_verdicts() -> None:
    builders = (
        build_automated_product_component,
        build_live_provider_component,
        build_multi_agent_panel_component,
        build_sealed_agent_blind_component,
    )
    forbidden = {
        "verdict",
        "executed_check_count",
        "failed_check_count",
        "amap_live_effect_count",
        "qwen_live_effect_count",
        "accepted_p0_count",
        "accepted_p1_count",
        "aggregate_metrics",
    }
    for builder in builders:
        assert forbidden.isdisjoint(inspect.signature(builder).parameters)


def test_goal_ref_and_automated_contract_are_not_caller_selected() -> None:
    final_parameters = inspect.signature(verify_agent_gate_pass).parameters
    automated_parameters = inspect.signature(build_automated_product_component).parameters
    assert "expected_goal_id" not in final_parameters
    assert "remote_ref" not in final_parameters
    assert "gate_contract_path" not in automated_parameters
    binding = json.loads(
        (REPOSITORY_ROOT / "docs/governance/current_goal_binding.json").read_text(
            encoding="utf-8"
        )
    )
    contract = REPOSITORY_ROOT / binding["automated_gate_contract_path"]
    assert hashlib.sha256(contract.read_bytes()).hexdigest() == (
        binding["automated_gate_contract_sha256"]
    )


def test_candidate_cannot_skip_to_g07_or_select_a_weaker_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _keys, _registry = _manifest_and_keys(tmp_path)
    repository = tmp_path / "skip-candidate"
    weak_contract = repository / "backend/eval_data/agent_gate_v1/weak.json"
    _write(
        weak_contract,
        {
            "schema_version": "automated-product-gate-contract-v1",
            "goal_id": "TC-VNEXT-G07-CANDIDATE",
            "checks": [],
        },
    )
    _write(
        repository / "docs/governance/current_goal_binding.json",
        {
            "schema_version": "current-goal-binding-v1",
            "program_id": "TC-VNEXT-2026",
            "goal_sequence": 7,
            "goal_id": "TC-VNEXT-G07-CANDIDATE",
            "status": "IN_PROGRESS",
            "canonical_candidate_ref": "refs/heads/codex/trip-check-product-reset",
            "predecessor_goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
            "predecessor_completion_commit": "1" * 40,
            "automated_gate_contract_path": "backend/eval_data/agent_gate_v1/weak.json",
            "automated_gate_contract_sha256": hashlib.sha256(
                weak_contract.read_bytes()
            ).hexdigest(),
        },
    )
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Goal Skip Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "attempt skip"],
        check=True,
        capture_output=True,
    )
    candidate = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(
        "evals.agent_gate_v1.authority._is_ancestor",
        lambda *_args, **_kwargs: True,
    )
    with pytest.raises(ValueError, match="matching authority generation"):
        load_current_goal_binding(repository, candidate, manifest)
    generation_seven = manifest.model_copy(update={"authority_generation": 7})
    with pytest.raises(ValueError, match="immutable Program transition table"):
        load_current_goal_binding(repository, candidate, generation_seven)


def test_external_goal_pass_registry_is_idempotent_and_required_for_next_goal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, keys, registry = _manifest_and_keys(tmp_path)
    from evals.agent_gate_v1 import custody as custody_module

    anchored = AnchoredAuthorityPolicy(
        manifest=manifest,
        content=b"policy",
        sha256="5" * 64,
        anchor_commit=SUBJECT,
        candidate_commit=SUBJECT,
    )
    g01 = CurrentGoalBinding.model_validate(
        {
            "goal_sequence": 1,
            "goal_id": GOAL_ID,
            "status": "IN_PROGRESS",
            "predecessor_goal_id": "TC-BP-G00-BLUEPRINT",
            "predecessor_completion_commit": "f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac",
            "automated_gate_contract_path": manifest.goal_bindings[0].automated_gate_contract_path,
            "automated_gate_contract_sha256": manifest.goal_bindings[0].automated_gate_contract_sha256,
        }
    )
    monkeypatch.setattr(
        custody_module,
        "load_anchored_authority_policy",
        lambda *_args, **_kwargs: anchored,
    )
    monkeypatch.setattr(custody_module, "require_formal_origin", lambda *_args: "origin")
    monkeypatch.setattr(
        custody_module,
        "load_current_goal_binding",
        lambda *_args, **_kwargs: g01,
    )
    monkeypatch.setattr(
        custody_module,
        "load_current_goal_document_state",
        lambda *_args, **_kwargs: (SimpleNamespace(), "0" * 64),
    )
    monkeypatch.setattr(
        custody_module,
        "git_blob_sha256",
        lambda *_args, **_kwargs: "a" * 64,
    )
    freeze_ref = (
        f"refs/heads/codex/agent-gate-candidates/g01-{SUBJECT}"
    )

    def goal_pass_git(_root, *args):
        if args[0] == "ls-remote":
            return f"{SUBJECT}\t{freeze_ref}"
        if args[:3] == ("show", "-s", "--format=%T"):
            return TREE
        raise AssertionError(args)

    monkeypatch.setattr(custody_module, "_git", goal_pass_git)
    monkeypatch.setattr(
        custody_module,
        "load_registered_authority_anchor",
        lambda **_kwargs: SimpleNamespace(receipt_sha256="b" * 64),
    )
    unsigned = {
        "schema_version": "agent-gate-pass-receipt-v2",
        "goal_sequence": 1,
        "goal_id": GOAL_ID,
        "predecessor_goal_id": g01.predecessor_goal_id,
        "predecessor_completion_commit": g01.predecessor_completion_commit,
        "current_goal_binding_sha256": "a" * 64,
        "current_goal_document_sha256": "0" * 64,
        "automated_gate_contract_sha256": g01.automated_gate_contract_sha256,
        "candidate_commit": SUBJECT,
        "candidate_tree": TREE,
        "authority_anchor_commit": SUBJECT,
        "authority_policy_sha256": "5" * 64,
        "authority_generation": 1,
        "authority_anchor_receipt_sha256": "b" * 64,
        "canonical_origin_url": "https://github.com/Munto47/BreezeTravel.git",
        "candidate_config_sha256": "c" * 64,
        "candidate_data_sha256": "d" * 64,
        "component_receipt_sha256": {
            "AUTOMATED_PRODUCT_GATE": "1" * 64,
            "LIVE_PROVIDER_GATE": "2" * 64,
            "MULTI_AGENT_PANEL": "3" * 64,
            "SEALED_AGENT_BLIND": "4" * 64,
        },
        "fresh_checkout_root_sha256": "e" * 64,
        "remote_name": "origin",
        "remote_ref": freeze_ref,
        "remote_subject": SUBJECT,
        "remote_tree": TREE,
        "verifier_sha256": "f" * 64,
        "evidence_levels": [
            "AUTOMATED_TEST",
            "LIVE_PROVIDER_EVIDENCE",
            "MULTI_AGENT_SIMULATED_REVIEW",
            "SEALED_AGENT_BLIND",
        ],
        "human_usability_status": "NOT_RUN",
        "production_status": "NOT_RUN",
        "verdict": "AGENT_GATE_PASS",
        "completed_at": "2026-08-28T00:00:00Z",
    }
    signature = sign_payload_for_development(
        payload=unsigned,
        private_key_path=keys["FINAL_GATE"],
        repository_root=REPOSITORY_ROOT,
        authority=next(item for item in manifest.authorities if item.role == "FINAL_GATE"),
    )
    receipt = AgentGatePassReceipt.model_validate(
        {**unsigned, "authority_signature": signature.model_dump(mode="json")}
    )
    materialized_receipt_path = tmp_path / "materialized-agent-gate-pass.json"
    _write(materialized_receipt_path, receipt.model_dump(mode="json"))
    first = register_goal_gate_pass(
        registry_path=registry,
        repository_root=REPOSITORY_ROOT,
        receipt=receipt,
        materialized_receipt_path=materialized_receipt_path,
    )
    second = register_goal_gate_pass(
        registry_path=registry,
        repository_root=REPOSITORY_ROOT,
        receipt=receipt,
        materialized_receipt_path=materialized_receipt_path,
    )
    assert first.receipt_sha256 == second.receipt_sha256
    recovered_path = tmp_path / "recovered-agent-gate-pass.json"
    recovered = recover_registered_goal_gate_pass(
        registry_path=registry,
        repository_root=REPOSITORY_ROOT,
        goal_sequence=1,
        output_path=recovered_path,
    )
    assert recovered["receipt_sha256"] == first.receipt_sha256
    assert recovered_path.read_bytes() == materialized_receipt_path.read_bytes()

    g02 = CurrentGoalBinding.model_validate(
        {
            "goal_sequence": 2,
            "goal_id": "TC-VNEXT-G02-MAP-STAY",
            "status": "APPROVED",
            "predecessor_goal_id": GOAL_ID,
            "predecessor_completion_commit": SUBJECT,
            "automated_gate_contract_path": manifest.goal_bindings[1].automated_gate_contract_path,
            "automated_gate_contract_sha256": manifest.goal_bindings[1].automated_gate_contract_sha256,
        }
    )
    generation_two = manifest.model_copy(update={"authority_generation": 2})
    generation_two_anchored = AnchoredAuthorityPolicy(
        manifest=generation_two,
        content=b"policy-generation-two",
        sha256="6" * 64,
        anchor_commit="1" * 40,
        candidate_commit="1" * 40,
    )
    previous = require_predecessor_goal_pass(
        registry_path=registry,
        repository_root=REPOSITORY_ROOT,
        anchored_policy=generation_two_anchored,
        current_binding=g02,
    )
    assert previous is not None and previous.receipt.goal_id == GOAL_ID

    empty_registry = tmp_path / "empty" / "registry.sqlite3"
    empty_registry.parent.mkdir()
    empty_identity = initialize_custody_registry(
        registry_path=empty_registry,
        repository_root=REPOSITORY_ROOT,
    )
    empty_manifest = generation_two.model_copy(
        update={
            "custody_registry_identity_sha256": empty_identity,
            "custody_registry_path_sha256": custody_module.canonical_path_sha256(
                empty_registry
            ),
        }
    )
    empty_anchored = anchored.__class__(
        manifest=empty_manifest,
        content=anchored.content,
        sha256=anchored.sha256,
        anchor_commit=anchored.anchor_commit,
        candidate_commit=anchored.candidate_commit,
    )
    with pytest.raises(SealedBlindCustodyError, match="no externally registered"):
        require_predecessor_goal_pass(
            registry_path=empty_registry,
            repository_root=REPOSITORY_ROOT,
            anchored_policy=empty_anchored,
            current_binding=g02,
        )


def test_recursive_binding_roots_cover_future_goal_files(tmp_path: Path) -> None:
    repository = tmp_path / "binding-repository"
    repository.mkdir()
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Binding Test"],
        check=True,
    )
    root = repository / "backend/app"
    root.mkdir(parents=True)
    (root / "g01.py").write_text("G01 = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "g01"],
        check=True,
        capture_output=True,
    )
    first = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    first_hash = compute_git_blob_bundle_hash(repository, first, ["backend/app"])
    (root / "g02.py").write_text("G02 = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "g02"],
        check=True,
        capture_output=True,
    )
    second = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    second_hash = compute_git_blob_bundle_hash(repository, second, ["backend/app"])
    assert first_hash != second_hash


def test_live_qwen_runtime_requires_http_database_and_output_provenance() -> None:
    value = {
        "schema_version": "g01-qwen-inference-receipt-bundle-v2",
        "goal_id": GOAL_ID,
        "dataset_version": "g01-text-card-dataset-v1",
        "split": "validation",
        "candidate_commit": SUBJECT,
        "candidate_tree": TREE,
        "provider": "QWEN",
        "execution_mode": "LIVE",
        "evidence_level": "LIVE_PROVIDER_EVIDENCE",
        "region": "cn-beijing",
        "endpoint_sha256": "1" * 64,
        "exact_model_id": "qwen-plus-snapshot",
        "model_binding_sha256": "2" * 64,
        "prompt_sha256": "3" * 64,
        "schema_sha256": "4" * 64,
        "config_sha256": "5" * 64,
        "provider_binding_sha256": "6" * 64,
        "predictions_sha256": "7" * 64,
        "inference_outputs_sha256": "8" * 64,
        "authority_policy_sha256": "9" * 64,
        "exporter_path": "backend/scripts/export_qwen_live_receipts.py",
        "exporter_sha256": "a" * 64,
        "generated_at": "2026-08-28T00:00:02Z",
        "generated_by": "G01_QWEN_LIVE_RECEIPT_EXPORTER",
        "raw_request_or_response_in_repository": False,
        "effects": [
            {
                "effect_id": "qwen-effect-0001",
                "case_id": "G01-TC-001",
                "input_sha256": "b" * 64,
                "request_sha256": "c" * 64,
                "response_sha256": "d" * 64,
                "provider_request_id_sha256": "e" * 64,
                "output_sha256": "f" * 64,
                "input_tokens": 10,
                "output_tokens": 10,
                "latency_ms": 10,
                "repair_call_count": 0,
                "started_at": "2026-08-28T00:00:00Z",
                "completed_at": "2026-08-28T00:00:01Z",
                "status": "SUCCEEDED",
            }
        ],
        "authority_signature": {
            "authority_role": "QWEN_LIVE_EXPORTER",
            "authority_id": "AUTH-QWEN-LIVE-EXPORTER-000001",
            "algorithm": "ED25519",
            "signed_payload_sha256": "1" * 64,
            "signature_base64": "A" * 88,
        },
    }
    with pytest.raises(ValueError, match="runtime provenance"):
        InferenceRuntimeReceiptBundle.model_validate(value)


def test_authority_anchor_registration_is_idempotent_and_conflict_rejecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, keys, registry = _manifest_and_keys(tmp_path)
    from evals.agent_gate_v1 import custody as custody_module

    anchored = AnchoredAuthorityPolicy(
        manifest=manifest,
        content=b"derived-policy",
        sha256="1" * 64,
        anchor_commit=SUBJECT,
        candidate_commit=SUBJECT,
    )
    monkeypatch.setattr(
        custody_module,
        "load_anchored_authority_policy",
        lambda *_args, **_kwargs: anchored,
    )
    monkeypatch.setattr(custody_module, "require_formal_origin", lambda *_args: "origin")
    monkeypatch.setattr(
        custody_module,
        "compute_git_blob_bundle_hash",
        lambda *_args, **_kwargs: "2" * 64,
    )
    g01_binding = CurrentGoalBinding.model_validate(
        {
            "goal_sequence": 1,
            "goal_id": GOAL_ID,
            "status": "IN_PROGRESS",
            "predecessor_goal_id": "TC-BP-G00-BLUEPRINT",
            "predecessor_completion_commit": (
                "f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac"
            ),
            "automated_gate_contract_path": (
                manifest.goal_bindings[0].automated_gate_contract_path
            ),
            "automated_gate_contract_sha256": (
                manifest.goal_bindings[0].automated_gate_contract_sha256
            ),
        }
    )
    monkeypatch.setattr(
        custody_module,
        "load_current_goal_binding",
        lambda *_args, **_kwargs: g01_binding,
    )

    def derived_git(_root, *args):
        if args[0] == "ls-remote":
            return f"{SUBJECT}\trefs/heads/codex/trip-check-product-reset"
        if args[:3] == ("show", "-s", "--format=%T"):
            return TREE
        raise AssertionError(args)

    monkeypatch.setattr(custody_module, "_git", derived_git)
    unsigned_anchor = {
        "schema_version": "agent-gate-authority-anchor-receipt-v1",
        "authority_generation": manifest.authority_generation,
        "canonical_candidate_ref": manifest.canonical_candidate_ref,
        "anchor_commit": SUBJECT,
        "anchor_tree": TREE,
        "authority_policy_sha256": "1" * 64,
        "program_core_sha256": "2" * 64,
        "immutable_protocol_sha256": "2" * 64,
        "public_key_set_sha256": compute_public_key_set_sha256(manifest),
        "custody_registry_identity_sha256": (
            manifest.custody_registry_identity_sha256
        ),
        "activated_at": "2026-08-28T00:00:01Z",
        "human_evidence": False,
    }
    anchor_signature = sign_payload_for_development(
        payload=unsigned_anchor,
        private_key_path=keys["SEALED_CUSTODY"],
        repository_root=REPOSITORY_ROOT,
        authority=next(
            item for item in manifest.authorities if item.role == "SEALED_CUSTODY"
        ),
    )
    signed_anchor = AuthorityAnchorReceipt.model_validate(
        {
            **unsigned_anchor,
            "authority_signature": anchor_signature.model_dump(mode="json"),
        }
    )
    values = {
        "registry_path": registry,
        "repository_root": REPOSITORY_ROOT,
        "candidate_commit": SUBJECT,
        "receipt": signed_anchor,
    }
    forbidden = {
        "manifest",
        "anchor_commit",
        "anchor_tree",
        "authority_policy_sha256",
        "immutable_protocol_sha256",
        "custody_private_key_path",
    }
    assert forbidden.isdisjoint(inspect.signature(register_authority_anchor).parameters)
    first = register_authority_anchor(**values)
    second = register_authority_anchor(**values)
    assert first == second
    original_git = custody_module._git

    def changed_tree(root, *args):
        if args[:3] == ("show", "-s", "--format=%T"):
            return "3" * 40
        return original_git(root, *args)

    monkeypatch.setattr(custody_module, "_git", changed_tree)
    with pytest.raises(SealedBlindCustodyError, match="derived candidate facts"):
        register_authority_anchor(**values)

    remote_reads = 0

    def moved_remote(_root, *args):
        nonlocal remote_reads
        if args[0] == "ls-remote":
            remote_reads += 1
            commit = SUBJECT if remote_reads == 1 else "4" * 40
            return f"{commit}\trefs/heads/codex/trip-check-product-reset"
        if args[:3] == ("show", "-s", "--format=%T"):
            return TREE
        raise AssertionError(args)

    monkeypatch.setattr(custody_module, "_git", moved_remote)
    with pytest.raises(SealedBlindCustodyError, match="does not match"):
        register_authority_anchor(**values)
    assert remote_reads == 2
    moved_registry = registry.with_name("registry-moved.sqlite3")
    registry.rename(moved_registry)
    moved_registry.rename(registry)


def test_bootstrap_sealed_scorer_has_no_secret_path_and_fails_closed() -> None:
    source = (BACKEND_ROOT / "scripts/score_g01_sealed_agent_blind.py").read_text(
        encoding="utf-8"
    )
    assert "KEY_PATH" not in source
    assert "private_key_path" not in source
    assert "NOT_RUN during BOOTSTRAP" in source
    assert "repository-external custodian scorer and signer IPC" in source


def test_every_goal_has_goal_specific_backend_and_browser_automation() -> None:
    for sequence in range(1, 8):
        contract = json.loads(
            (
                GENERAL_ROOT / f"g{sequence:02d}_automated_product_gate.json"
            ).read_text(encoding="utf-8")
        )
        goal_checks = [
            check
            for check in contract["checks"]
            if check["check_id"].startswith(f"g{sequence:02d}.")
        ]
        assert goal_checks, f"G{sequence:02d} has no Goal-specific checks"
        assert any(
            check["argv"][:3] == ["python", "-m", "pytest"]
            for check in goal_checks
        ), f"G{sequence:02d} has no Goal-specific backend check"
        assert any(
            check["argv"][:2] == ["npm", "run"]
            and "browser" in check["check_id"]
            for check in goal_checks
        ), f"G{sequence:02d} has no Goal-specific browser check"


def test_automation_runner_is_credential_free_and_service_self_contained() -> None:
    dockerfile = (
        GENERAL_ROOT / "automation_runner.Dockerfile"
    ).read_text(encoding="utf-8")
    entrypoint = (
        GENERAL_ROOT / "automation_runner_entrypoint.sh"
    ).read_text(encoding="utf-8")
    context_policy = (
        GENERAL_ROOT / "automation_runner.Dockerfile.dockerignore"
    ).read_text(encoding="utf-8")
    isolation_source = (
        BACKEND_ROOT / "evals/agent_gate_v1/automation_isolation.py"
    ).read_text(encoding="utf-8")

    assert "pgvector/pgvector:0.8.1-pg16" in dockerfile
    assert "automation_runner_requirements.lock" in dockerfile
    assert "automation_runner_browser_package-lock.json" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "snapshot.debian.org/archive/debian/20250820T000000Z" in dockerfile
    assert "@sha256:" in dockerfile
    assert (
        "/opt/breezetravel-agent-gate-browser/node_modules/.bin/playwright "
        "install --with-deps chromium"
    ) in dockerfile
    assert "npm --prefix /workspace/frontend exec" not in dockerfile
    assert "FROM node_runtime AS candidate_dependencies" in dockerfile
    assert "USER node" in dockerfile
    assert "COPY --from=candidate_git candidate.pack" in dockerfile
    assert "git -C /workspace index-pack --stdin" in dockerfile
    assert "USER postgres" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/breezetravel-agent-gate-entrypoint"]' in dockerfile
    assert "RUN_SERVICE_INTEGRATION=1" in entrypoint
    assert "E2E_BASE_URL=http://127.0.0.1:3000" in entrypoint
    assert "python -m app.trip_understanding.worker" in entrypoint
    assert "python -m app.trip_understanding.map_worker" in entrypoint
    assert "test_agent_gate_live_registry_postgres.py" in entrypoint
    assert "DASHSCOPE_API_KEY" not in entrypoint
    assert "AMAP_API_KEY" not in entrypoint
    assert context_policy.strip() == "**"
    assert "COPY --chown=postgres:postgres . /workspace" not in dockerfile
    assert "source/ /workspace" in dockerfile
    assert '"--network=none"' in isolation_source
    assert '"--cap-drop=ALL"' in isolation_source
    assert '"--build-context"' in isolation_source
    assert "_materialize_candidate_git_context(" in isolation_source
    assert '"--volume"' not in isolation_source
    assert '"--mount"' not in isolation_source


def test_candidate_git_context_reconstructs_exact_commit_and_tree(tmp_path: Path) -> None:
    context = tmp_path / "candidate-git"
    pack_sha256 = _materialize_candidate_git_context(
        repository_root=REPOSITORY_ROOT,
        candidate_commit=SUBJECT,
        candidate_tree=TREE,
        destination=context,
    )
    assert len(pack_sha256) == 64
    assert hashlib.sha256((context / "candidate.pack").read_bytes()).hexdigest() == pack_sha256
    assert not (context / "source" / ".git").exists()
    expected_agents = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "show", f"{SUBJECT}:AGENTS.md"],
        check=True,
        capture_output=True,
    ).stdout
    assert (context / "source" / "AGENTS.md").read_bytes() == expected_agents

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
    with (context / "candidate.pack").open("rb") as pack_handle:
        subprocess.run(
            ["git", "-C", str(checkout), "index-pack", "--stdin"],
            stdin=pack_handle,
            check=True,
            capture_output=True,
        )
    (checkout / ".git" / "shallow").write_bytes(
        (context / "candidate.shallow").read_bytes()
    )
    subprocess.run(
        ["git", "-C", str(checkout), "update-ref", "HEAD", SUBJECT],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "reset", "--hard", SUBJECT],
        check=True,
        capture_output=True,
    )
    recovered = subprocess.run(
        ["git", "-C", str(checkout), "show", "-s", "--format=%H:%T", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert recovered == f"{SUBJECT}:{TREE}"


def test_candidate_context_excludes_ignored_and_deleted_intermediate_blobs(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    for key, value in (
        ("user.name", "Agent Gate Test"),
        ("user.email", "agent-gate@example.invalid"),
        ("core.autocrlf", "false"),
    ):
        subprocess.run(
            ["git", "-C", str(repository), "config", key, value],
            check=True,
        )
    (repository / ".gitignore").write_text("ignored-local.bin\n", encoding="utf-8")
    (repository / "stable.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "baseline"],
        check=True,
    )
    baseline = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    historical_secret = repository / "historical-secret.bin"
    historical_secret.write_bytes(b"must-not-enter-candidate-image")
    secret_blob = subprocess.run(
        ["git", "-C", str(repository), "hash-object", "historical-secret.bin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repository), "add", "historical-secret.bin"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "temporary"],
        check=True,
    )
    historical_secret.unlink()
    subprocess.run(
        ["git", "-C", str(repository), "add", "-u"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "candidate"],
        check=True,
    )
    (repository / "ignored-local.bin").write_bytes(b"host-only-data")
    assert subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain=v1"],
        check=True,
        capture_output=True,
    ).stdout == b""
    candidate = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    candidate_tree = subprocess.run(
        ["git", "-C", str(repository), "show", "-s", "--format=%T", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    context = tmp_path / "candidate-context"
    _materialize_candidate_git_context(
        repository_root=repository,
        candidate_commit=candidate,
        candidate_tree=candidate_tree,
        destination=context,
        shallow_baseline_commit=baseline,
    )
    assert not (context / "source" / "ignored-local.bin").exists()
    assert not (context / "source" / "historical-secret.bin").exists()

    object_store = tmp_path / "object-store"
    subprocess.run(["git", "init", "--quiet", str(object_store)], check=True)
    with (context / "candidate.pack").open("rb") as pack_handle:
        subprocess.run(
            ["git", "-C", str(object_store), "index-pack", "--stdin"],
            stdin=pack_handle,
            check=True,
            capture_output=True,
        )
    missing_secret = subprocess.run(
        ["git", "-C", str(object_store), "cat-file", "-e", secret_blob],
        check=False,
        capture_output=True,
    )
    assert missing_secret.returncode != 0


def test_missing_local_automation_image_loads_exact_signed_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    archive = tmp_path / "runner-image.tar"
    archive_bytes, expected_image_id = _docker_image_archive_bytes()
    archive.write_bytes(archive_bytes)
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    docker_calls: list[list[str]] = []
    loaded_content: list[bytes] = []

    def docker(args, **_kwargs):
        docker_calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=expected_image_id.encode("utf-8"),
            )
        if args[:2] == ["image", "load"]:
            loaded_content.append(_kwargs["stdin"].read())
            return SimpleNamespace(
                returncode=0,
                stdout=f"Loaded image ID: {expected_image_id}\n".encode("ascii"),
            )
        raise AssertionError(args)

    monkeypatch.setattr(isolation_module, "_run_docker", docker)
    observed = ensure_isolated_candidate_image(
        repository_root=REPOSITORY_ROOT,
        expected_image_id=expected_image_id,
        image_archive_path=archive,
        image_archive_sha256=archive_sha256,
        image_archive_size=archive.stat().st_size,
    )
    assert observed == expected_image_id
    assert loaded_content == [archive.read_bytes()]
    assert [call[:3] for call in docker_calls] == [
        ["image", "load"],
        ["image", "inspect", "--format={{.Id}}"],
    ]

    with pytest.raises(AutomationIsolationError, match="archive binding mismatch"):
        ensure_isolated_candidate_image(
            repository_root=REPOSITORY_ROOT,
            expected_image_id=expected_image_id,
            image_archive_path=archive,
            image_archive_sha256="9" * 64,
            image_archive_size=archive.stat().st_size,
        )


def test_cached_automation_tag_cannot_bypass_archive_load_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    archive = tmp_path / "runner-image.tar"
    _valid_archive, expected_image_id = _docker_image_archive_bytes()
    archive.write_bytes(b"not a valid docker archive")
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    docker_calls: list[list[str]] = []

    def docker(args, **_kwargs):
        docker_calls.append(args)
        if args[:2] == ["image", "load"]:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"invalid")
        if args[:2] == ["image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=expected_image_id.encode())
        raise AssertionError(args)

    monkeypatch.setattr(isolation_module, "_run_docker", docker)
    with pytest.raises(AutomationIsolationError, match="not a valid Docker tar"):
        ensure_isolated_candidate_image(
            repository_root=REPOSITORY_ROOT,
            expected_image_id=expected_image_id,
            image_archive_path=archive,
            image_archive_sha256=archive_sha256,
            image_archive_size=archive.stat().st_size,
        )
    assert docker_calls == []


def test_multi_image_automation_archive_is_rejected_before_docker_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    archive_bytes, expected_image_id = _docker_image_archive_bytes(
        extra_manifest_entry=True,
    )
    archive = tmp_path / "runner-image.tar"
    archive.write_bytes(archive_bytes)
    docker_calls: list[list[str]] = []

    def docker(args, **_kwargs):
        docker_calls.append(args)
        raise AssertionError(args)

    monkeypatch.setattr(isolation_module, "_run_docker", docker)
    with pytest.raises(
        AutomationIsolationError,
        match="exactly one image",
    ):
        ensure_isolated_candidate_image(
            repository_root=REPOSITORY_ROOT,
            expected_image_id=expected_image_id,
            image_archive_path=archive,
            image_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            image_archive_size=len(archive_bytes),
        )
    assert docker_calls == []


def test_tagged_automation_archive_is_rejected_before_docker_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    archive_bytes, expected_image_id = _docker_image_archive_bytes(
        repo_tags=["unexpected/image:tag"],
    )
    archive = tmp_path / "runner-image.tar"
    archive.write_bytes(archive_bytes)
    docker_calls: list[list[str]] = []

    def docker(args, **_kwargs):
        docker_calls.append(args)
        raise AssertionError(args)

    monkeypatch.setattr(isolation_module, "_run_docker", docker)
    with pytest.raises(AutomationIsolationError, match="expected image"):
        ensure_isolated_candidate_image(
            repository_root=REPOSITORY_ROOT,
            expected_image_id=expected_image_id,
            image_archive_path=archive,
            image_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            image_archive_size=len(archive_bytes),
        )
    assert docker_calls == []


def test_legacy_archive_layers_must_match_config_diff_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    archive_bytes, expected_image_id = _docker_image_archive_bytes(
        invalid_layer_binding=True,
    )
    archive = tmp_path / "runner-image.tar"
    archive.write_bytes(archive_bytes)
    docker_calls: list[list[str]] = []

    def docker(args, **_kwargs):
        docker_calls.append(args)
        raise AssertionError(args)

    monkeypatch.setattr(isolation_module, "_run_docker", docker)
    with pytest.raises(AutomationIsolationError, match="layer bindings"):
        ensure_isolated_candidate_image(
            repository_root=REPOSITORY_ROOT,
            expected_image_id=expected_image_id,
            image_archive_path=archive,
            image_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            image_archive_size=len(archive_bytes),
        )
    assert docker_calls == []


def test_single_primary_oci_archive_loads_and_binds_legacy_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    archive_bytes, expected_image_id = _oci_docker_image_archive_bytes()
    archive = tmp_path / "runner-image.tar"
    archive.write_bytes(archive_bytes)

    def docker(args, **_kwargs):
        if args[:2] == ["image", "load"]:
            return SimpleNamespace(
                returncode=0,
                stdout=f"Loaded image ID: {expected_image_id}\n".encode("ascii"),
                stderr=b"",
            )
        if args[:2] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=expected_image_id.encode("ascii"),
                stderr=b"",
            )
        raise AssertionError(args)

    monkeypatch.setattr(isolation_module, "_run_docker", docker)
    assert ensure_isolated_candidate_image(
        repository_root=REPOSITORY_ROOT,
        expected_image_id=expected_image_id,
        image_archive_path=archive,
        image_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        image_archive_size=len(archive_bytes),
    ) == expected_image_id


def test_runnable_image_cannot_masquerade_as_oci_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    archive_bytes, expected_image_id = _oci_docker_image_archive_bytes(
        masquerading_attestation=True,
    )
    archive = tmp_path / "runner-image.tar"
    archive.write_bytes(archive_bytes)
    docker_calls: list[list[str]] = []

    def docker(args, **_kwargs):
        docker_calls.append(args)
        raise AssertionError(args)

    monkeypatch.setattr(isolation_module, "_run_docker", docker)
    with pytest.raises(
        AutomationIsolationError,
        match="attestation (?:layer|config)|legacy and OCI graphs disagree",
    ):
        ensure_isolated_candidate_image(
            repository_root=REPOSITORY_ROOT,
            expected_image_id=expected_image_id,
            image_archive_path=archive,
            image_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            image_archive_size=len(archive_bytes),
        )
    assert docker_calls == []


def test_archive_load_report_must_match_the_signed_image_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    archive_bytes, expected_image_id = _docker_image_archive_bytes()
    archive = tmp_path / "runner-image.tar"
    archive.write_bytes(archive_bytes)
    docker_calls: list[list[str]] = []

    def docker(args, **_kwargs):
        docker_calls.append(args)
        if args[:2] == ["image", "load"]:
            return SimpleNamespace(
                returncode=0,
                stdout=f"Loaded image ID: sha256:{'9' * 64}\n".encode("ascii"),
                stderr=b"",
            )
        raise AssertionError(args)

    monkeypatch.setattr(isolation_module, "_run_docker", docker)
    with pytest.raises(AutomationIsolationError, match="different image identity"):
        ensure_isolated_candidate_image(
            repository_root=REPOSITORY_ROOT,
            expected_image_id=expected_image_id,
            image_archive_path=archive,
            image_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            image_archive_size=len(archive_bytes),
        )
    assert [call[:2] for call in docker_calls] == [["image", "load"]]


def test_archive_path_replacement_after_snapshot_cannot_change_docker_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    archive_bytes, expected_image_id = _docker_image_archive_bytes()
    archive = tmp_path / "runner-image.tar"
    archive.write_bytes(archive_bytes)
    original_copy = isolation_module.copy_external_file_to_stream_verified
    docker_input: list[bytes] = []

    def copy_then_replace(*args, **kwargs):
        result = original_copy(*args, **kwargs)
        archive.write_bytes(b"replacement after stable snapshot")
        return result

    def docker(args, **kwargs):
        if args[:2] == ["image", "load"]:
            docker_input.append(kwargs["stdin"].read())
            return SimpleNamespace(
                returncode=0,
                stdout=f"Loaded image ID: {expected_image_id}\n".encode("ascii"),
                stderr=b"",
            )
        if args[:2] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=expected_image_id.encode("ascii"),
                stderr=b"",
            )
        raise AssertionError(args)

    monkeypatch.setattr(
        isolation_module,
        "copy_external_file_to_stream_verified",
        copy_then_replace,
    )
    monkeypatch.setattr(isolation_module, "_run_docker", docker)
    observed = ensure_isolated_candidate_image(
        repository_root=REPOSITORY_ROOT,
        expected_image_id=expected_image_id,
        image_archive_path=archive,
        image_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        image_archive_size=len(archive_bytes),
    )
    assert observed == expected_image_id
    assert docker_input == [archive_bytes]
    assert archive.read_bytes() != archive_bytes


def test_automation_image_archive_is_published_externally_and_hashed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals.agent_gate_v1 import automation_isolation as isolation_module

    archive = tmp_path / "runner-image.tar"
    image_tag = "breezetravel-agent-gate:test"
    content, expected_image_id = _docker_image_archive_bytes()
    docker_calls: list[list[str]] = []

    def docker(args, **_kwargs):
        docker_calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=expected_image_id.encode("ascii"),
                stderr=b"",
            )
        assert args == ["image", "save", expected_image_id]
        _kwargs["stdout"].write(content)
        return SimpleNamespace(returncode=0, stdout=None, stderr=b"")

    monkeypatch.setattr(isolation_module, "_run_docker", docker)
    snapshot = save_isolated_candidate_image(
        repository_root=REPOSITORY_ROOT,
        image_tag=image_tag,
        expected_image_id=expected_image_id,
        archive_output=archive,
    )
    assert snapshot.path == archive.resolve()
    assert snapshot.sha256 == hashlib.sha256(content).hexdigest()
    assert snapshot.size == len(content)
    assert archive.read_bytes() == content
    assert docker_calls == [
        ["image", "inspect", "--format={{.Id}}", image_tag],
        ["image", "save", expected_image_id],
    ]


def test_final_pass_rechecks_immutable_remote_and_materializes_before_registry() -> None:
    final_source = (
        BACKEND_ROOT / "evals/agent_gate_v1/final_gate.py"
    ).read_text(encoding="utf-8")
    # One definition plus two calls: before long verification and immediately
    # before signing. The external registry is updated only after durable bytes.
    assert final_source.count("_read_remote_candidate(") == 3
    materialize_index = final_source.index("write_external_bytes_exclusive(")
    register_index = final_source.index("register_goal_gate_pass(", materialize_index)
    assert materialize_index < register_index
