from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evals.agent_gate_v1.authority import (
    AnchoredAuthorityPolicy,
    candidate_freeze_ref,
    compute_git_blob_bundle_hash,
    compute_public_key_set_sha256,
    git_blob_sha256,
    load_anchored_authority_policy,
    load_current_goal_binding,
    load_current_goal_document_state,
    require_formal_origin,
    require_scoped_goal,
)
from evals.agent_gate_v1.contracts import (
    AgentGatePassReceipt,
    AgentGateAuthorityManifest,
    AuthorityAnchorReceipt,
    SealedAgentBlindMintReceipt,
)
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1.path_security import (
    ArtifactSnapshot,
    read_external_snapshot,
    require_external_existing,
    require_external_target,
    write_external_bytes_exclusive,
)
from evals.agent_gate_v1.signing import (
    unsigned_payload,
    verify_payload_signature,
)


class SealedBlindCustodyError(ValueError):
    pass


SQLITE_BUSY_TIMEOUT_MS = 65_000


def _connect_registry(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return connection


def _begin_immediate(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise SealedBlindCustodyError(
                "custody registry is busy; retry the same idempotent request"
            ) from exc
        raise


@contextmanager
def _registry_transaction(path: Path, *, immediate: bool = False):
    """Close deterministically and normalize busy errors across the whole transaction."""

    with closing(_connect_registry(path)) as connection:
        try:
            if immediate:
                _begin_immediate(connection)
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            if connection.in_transaction:
                connection.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise SealedBlindCustodyError(
                    "custody registry is busy; retry the same idempotent request"
                ) from exc
            raise
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise


@dataclass(frozen=True)
class ConsumedSealedRun:
    mint: SealedAgentBlindMintReceipt
    mint_snapshot: ArtifactSnapshot
    attempt_commitment_sha256: str


@dataclass(frozen=True)
class RegisteredAuthorityAnchor:
    receipt: AuthorityAnchorReceipt
    receipt_sha256: str


@dataclass(frozen=True)
class RegisteredGoalGatePass:
    receipt: AgentGatePassReceipt
    receipt_sha256: str


def canonical_path_sha256(path: Path) -> str:
    value = os.path.normcase(str(path.resolve())).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _serialize(value: SealedAgentBlindMintReceipt) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            [trusted_host_tool("git"), "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise SealedBlindCustodyError("Git custody readback timed out") from exc
    if result.returncode != 0:
        raise SealedBlindCustodyError(
            f"Git custody readback failed: {' '.join(args)}"
        )
    return result.stdout.strip()


def _serialize_pass(value: AgentGatePassReceipt) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _require_trusted_transition_launcher(
    *,
    repository_root: Path,
    candidate_manifest: AgentGateAuthorityManifest,
    predecessor_pass: RegisteredGoalGatePass | None,
) -> None:
    if candidate_manifest.authority_generation == 1:
        if predecessor_pass is not None:
            raise SealedBlindCustodyError("generation 1 cannot have a Goal predecessor PASS")
        return
    if predecessor_pass is None:
        raise SealedBlindCustodyError(
            "later authority generations require a predecessor PASS launcher"
        )
    launcher_root = Path(__file__).resolve(strict=True).parents[3]
    previous_commit = predecessor_pass.receipt.candidate_commit
    if _git(launcher_root, "rev-parse", "HEAD") != previous_commit:
        raise SealedBlindCustodyError(
            "next authority generation must be registered by the previous anchored checkout"
        )
    if _git(launcher_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise SealedBlindCustodyError("authority transition launcher checkout is not clean")
    previous = load_anchored_authority_policy(launcher_root, previous_commit)
    require_formal_origin(launcher_root, previous.manifest)
    if previous.manifest.program_core_paths != candidate_manifest.program_core_paths:
        raise SealedBlindCustodyError("Program trust core path set changed across generations")
    for relative_path in previous.manifest.program_core_paths:
        disk_path = (launcher_root / relative_path).resolve(strict=True)
        try:
            disk_path.relative_to(launcher_root)
        except ValueError as exc:
            raise SealedBlindCustodyError(
                "Program trust core escaped the previous launcher checkout"
            ) from exc
        if hashlib.sha256(disk_path.read_bytes()).hexdigest() != git_blob_sha256(
            launcher_root,
            previous_commit,
            relative_path,
        ):
            raise SealedBlindCustodyError(
                f"previous authority launcher has modified Program core bytes: {relative_path}"
            )
    if repository_root.resolve(strict=True) == launcher_root:
        raise SealedBlindCustodyError(
            "authority transition candidate must be inspected from a distinct checkout"
        )


def _require_remote_freeze_ref(
    *,
    repository_root: Path,
    remote_ref: str,
    candidate_commit: str,
) -> None:
    lines = _git(
        repository_root,
        "ls-remote",
        "--refs",
        "origin",
        remote_ref,
    ).splitlines()
    if len(lines) != 1:
        raise SealedBlindCustodyError(
            "immutable candidate ref did not resolve exactly once"
        )
    subject, observed_ref = lines[0].split(maxsplit=1)
    if observed_ref != remote_ref or subject != candidate_commit:
        raise SealedBlindCustodyError(
            "immutable candidate ref does not match the PASS candidate"
        )


def initialize_custody_registry(*, registry_path: Path, repository_root: Path) -> str:
    target = write_external_bytes_exclusive(
        registry_path,
        b"",
        repository_root,
    ).path
    identity = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    try:
        with _registry_transaction(target) as connection:
            connection.executescript(
                """
                CREATE TABLE custody_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    registry_identity_sha256 TEXT NOT NULL,
                    canonical_path_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE sealed_runs (
                    mint_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id TEXT NOT NULL,
                    candidate_commit TEXT NOT NULL,
                    candidate_tree TEXT NOT NULL,
                    tranche_commitment_sha256 TEXT NOT NULL,
                    one_shot_nonce_sha256 TEXT NOT NULL UNIQUE,
                    custodian_task_id TEXT NOT NULL,
                    authority_policy_sha256 TEXT NOT NULL,
                    mint_receipt_bytes BLOB NOT NULL,
                    mint_receipt_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('MINTED', 'CONSUMED', 'COMPLETED')
                    ),
                    minted_at TEXT NOT NULL,
                    consumed_at TEXT,
                    completed_at TEXT,
                    attempt_commitment_sha256 TEXT,
                    attempt_receipt_sha256 TEXT,
                    score_input_manifest_sha256 TEXT,
                    score_receipt_sha256 TEXT,
                    UNIQUE(goal_id, candidate_commit, tranche_commitment_sha256)
                );
                CREATE TABLE authority_anchors (
                    authority_generation INTEGER PRIMARY KEY,
                    anchor_receipt_bytes BLOB NOT NULL,
                    anchor_receipt_sha256 TEXT NOT NULL UNIQUE,
                    activated_at TEXT NOT NULL
                );
                CREATE TABLE goal_gate_passes (
                    goal_sequence INTEGER PRIMARY KEY CHECK (
                        goal_sequence BETWEEN 1 AND 7
                    ),
                    goal_id TEXT NOT NULL UNIQUE,
                    candidate_commit TEXT NOT NULL UNIQUE,
                    candidate_tree TEXT NOT NULL,
                    pass_receipt_bytes BLOB NOT NULL,
                    pass_receipt_sha256 TEXT NOT NULL UNIQUE,
                    canonical_candidate_ref TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO custody_metadata (
                    singleton, registry_identity_sha256, canonical_path_sha256, created_at
                ) VALUES (1, ?, ?, ?)
                """,
                (
                    identity,
                    canonical_path_sha256(target),
                    _utc_now_text(),
                ),
            )
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return identity


def migrate_custody_registry_schema(
    *,
    registry_path: Path,
    repository_root: Path,
    manifest: AgentGateAuthorityManifest,
) -> dict[str, object]:
    registry = require_external_existing(registry_path, repository_root)
    with _registry_transaction(registry, immediate=True) as connection:
        identity, path_sha256 = _read_registry_metadata(connection, registry)
        _assert_registry_matches_policy(
            identity=identity,
            path_sha256=path_sha256,
            manifest=manifest,
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(sealed_runs)").fetchall()
        }
        if "attempt_commitment_sha256" not in columns:
            connection.execute(
                "ALTER TABLE sealed_runs ADD COLUMN attempt_commitment_sha256 TEXT"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS authority_anchors (
                authority_generation INTEGER PRIMARY KEY,
                anchor_receipt_bytes BLOB NOT NULL,
                anchor_receipt_sha256 TEXT NOT NULL UNIQUE,
                activated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS goal_gate_passes (
                goal_sequence INTEGER PRIMARY KEY CHECK (
                    goal_sequence BETWEEN 1 AND 7
                ),
                goal_id TEXT NOT NULL UNIQUE,
                candidate_commit TEXT NOT NULL UNIQUE,
                candidate_tree TEXT NOT NULL,
                pass_receipt_bytes BLOB NOT NULL,
                pass_receipt_sha256 TEXT NOT NULL UNIQUE,
                canonical_candidate_ref TEXT NOT NULL,
                registered_at TEXT NOT NULL
            )
            """
        )
        final_columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(sealed_runs)").fetchall()
        ]
        anchor_count = int(
            connection.execute("SELECT COUNT(*) FROM authority_anchors").fetchone()[0]
        )
        goal_gate_pass_count = int(
            connection.execute("SELECT COUNT(*) FROM goal_gate_passes").fetchone()[0]
        )
    return {
        "registry_identity_sha256": identity,
        "sealed_runs_columns": final_columns,
        "authority_anchor_count": anchor_count,
        "goal_gate_pass_count": goal_gate_pass_count,
    }


def register_authority_anchor(
    *,
    registry_path: Path,
    repository_root: Path,
    candidate_commit: str,
    receipt: AuthorityAnchorReceipt,
) -> dict[str, str]:
    anchored = load_anchored_authority_policy(repository_root, candidate_commit)
    manifest = anchored.manifest
    require_formal_origin(repository_root, manifest)
    current_binding = load_current_goal_binding(
        repository_root,
        candidate_commit,
        manifest,
    )
    predecessor_pass = require_predecessor_goal_pass(
        registry_path=registry_path,
        repository_root=repository_root,
        anchored_policy=anchored,
        current_binding=current_binding,
    )
    _require_trusted_transition_launcher(
        repository_root=repository_root,
        candidate_manifest=manifest,
        predecessor_pass=predecessor_pass,
    )

    def require_same_remote_candidate() -> None:
        remote_lines = _git(
            repository_root,
            "ls-remote",
            "--refs",
            "origin",
            manifest.canonical_candidate_ref,
        ).splitlines()
        if len(remote_lines) != 1:
            raise SealedBlindCustodyError(
                "canonical authority anchor ref did not resolve exactly once"
            )
        remote_commit, remote_ref = remote_lines[0].split(maxsplit=1)
        if (
            remote_ref != manifest.canonical_candidate_ref
            or remote_commit != candidate_commit
        ):
            raise SealedBlindCustodyError(
                "canonical authority anchor ref does not match the candidate"
            )

    require_same_remote_candidate()
    anchor_commit = anchored.anchor_commit
    anchor_tree = _git(repository_root, "show", "-s", "--format=%T", anchor_commit)
    authority_policy_sha256 = anchored.sha256
    program_core_sha256 = compute_git_blob_bundle_hash(
        repository_root,
        candidate_commit,
        manifest.program_core_paths,
    )
    immutable_protocol_sha256 = compute_git_blob_bundle_hash(
        repository_root,
        candidate_commit,
        manifest.immutable_protocol_paths,
    )
    registry = require_external_existing(registry_path, repository_root)
    expected = (
        "agent-gate-authority-anchor-receipt-v1",
        manifest.authority_generation,
        manifest.canonical_candidate_ref,
        anchor_commit,
        anchor_tree,
        authority_policy_sha256,
        program_core_sha256,
        immutable_protocol_sha256,
        compute_public_key_set_sha256(manifest),
        manifest.custody_registry_identity_sha256,
    )
    observed = (
        receipt.schema_version,
        receipt.authority_generation,
        receipt.canonical_candidate_ref,
        receipt.anchor_commit,
        receipt.anchor_tree,
        receipt.authority_policy_sha256,
        receipt.program_core_sha256,
        receipt.immutable_protocol_sha256,
        receipt.public_key_set_sha256,
        receipt.custody_registry_identity_sha256,
    )
    if observed != expected or receipt.activated_at < manifest.frozen_at:
        raise SealedBlindCustodyError(
            "signed authority anchor does not match derived candidate facts"
        )
    try:
        verify_payload_signature(
            payload=unsigned_payload(receipt),
            signature=receipt.authority_signature,
            manifest=manifest,
            expected_role="SEALED_CUSTODY",
        )
    except ValueError as exc:
        raise SealedBlindCustodyError(
            "signed authority anchor signature is invalid"
        ) from exc
    receipt_bytes = (
        json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    with _registry_transaction(registry, immediate=True) as connection:
        identity, path_sha256 = _read_registry_metadata(connection, registry)
        _assert_registry_matches_policy(
            identity=identity,
            path_sha256=path_sha256,
            manifest=manifest,
        )
        existing = connection.execute(
            """
            SELECT anchor_receipt_bytes, anchor_receipt_sha256
            FROM authority_anchors WHERE authority_generation = ?
            """,
            (manifest.authority_generation,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO authority_anchors (
                    authority_generation, anchor_receipt_bytes,
                    anchor_receipt_sha256, activated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    manifest.authority_generation,
                    receipt_bytes,
                    receipt_sha256,
                    receipt.activated_at.isoformat(),
                ),
            )
        else:
            existing_bytes = bytes(existing[0])
            existing_sha256 = hashlib.sha256(existing_bytes).hexdigest()
            if existing_sha256 != str(existing[1]):
                raise SealedBlindCustodyError("stored authority anchor hash mismatch")
            try:
                existing_receipt = AuthorityAnchorReceipt.model_validate_json(
                    existing_bytes
                )
            except ValueError as exc:
                raise SealedBlindCustodyError(
                    "stored authority anchor receipt is invalid"
                ) from exc
            verify_payload_signature(
                payload=unsigned_payload(existing_receipt),
                signature=existing_receipt.authority_signature,
                manifest=manifest,
                expected_role="SEALED_CUSTODY",
            )
            existing_binding = (
                existing_receipt.authority_generation,
                existing_receipt.canonical_candidate_ref,
                existing_receipt.anchor_commit,
                existing_receipt.anchor_tree,
                existing_receipt.authority_policy_sha256,
                existing_receipt.program_core_sha256,
                existing_receipt.immutable_protocol_sha256,
                existing_receipt.public_key_set_sha256,
                existing_receipt.custody_registry_identity_sha256,
            )
            requested_binding = (
                manifest.authority_generation,
                manifest.canonical_candidate_ref,
                anchor_commit,
                anchor_tree,
                authority_policy_sha256,
                program_core_sha256,
                immutable_protocol_sha256,
                compute_public_key_set_sha256(manifest),
                manifest.custody_registry_identity_sha256,
            )
            if existing_binding != requested_binding:
                raise SealedBlindCustodyError(
                    "authority generation is already anchored to different facts"
                )
            receipt_sha256 = existing_sha256
        # Re-read the remote only after all derived bytes and registry checks
        # are complete. A ref move invalidates this transaction before commit.
        require_same_remote_candidate()
        connection.commit()
    return {
        "authority_anchor_receipt_sha256": receipt_sha256,
        "anchor_commit": anchor_commit,
        "candidate_commit": candidate_commit,
    }


def require_predecessor_goal_pass(
    *,
    registry_path: Path,
    repository_root: Path,
    anchored_policy: AnchoredAuthorityPolicy,
    current_binding,
) -> RegisteredGoalGatePass | None:
    manifest = anchored_policy.manifest
    if current_binding.goal_sequence == 1:
        expected = manifest.goal_bindings[0].initial_predecessor_completion_commit
        if current_binding.predecessor_completion_commit != expected:
            raise SealedBlindCustodyError(
                "G01 predecessor does not match the immutable Blueprint activation"
            )
        return None

    registry = require_external_existing(registry_path, repository_root)
    with _registry_transaction(registry) as connection:
        identity, path_sha256 = _read_registry_metadata(connection, registry)
        _assert_registry_matches_policy(
            identity=identity,
            path_sha256=path_sha256,
            manifest=manifest,
        )
        row = connection.execute(
            """
            SELECT pass_receipt_bytes, pass_receipt_sha256
            FROM goal_gate_passes WHERE goal_sequence = ?
            """,
            (current_binding.goal_sequence - 1,),
        ).fetchone()
    if row is None:
        raise SealedBlindCustodyError(
            "the previous Goal has no externally registered AGENT_GATE_PASS"
        )
    content = bytes(row[0])
    observed_sha256 = hashlib.sha256(content).hexdigest()
    if observed_sha256 != str(row[1]):
        raise SealedBlindCustodyError("stored previous Goal PASS hash mismatch")
    try:
        receipt = AgentGatePassReceipt.model_validate_json(content)
    except ValueError as exc:
        raise SealedBlindCustodyError("stored previous Goal PASS is invalid") from exc
    previous_anchored = load_anchored_authority_policy(
        repository_root,
        receipt.candidate_commit,
    )
    previous_manifest = previous_anchored.manifest
    verify_payload_signature(
        payload=unsigned_payload(receipt),
        signature=receipt.authority_signature,
        manifest=previous_manifest,
        expected_role="FINAL_GATE",
    )
    registered_anchor = load_registered_authority_anchor(
        registry_path=registry_path,
        repository_root=repository_root,
        manifest=previous_manifest,
    )
    expected_previous = manifest.goal_bindings[current_binding.goal_sequence - 2]
    if (
        manifest.authority_generation != current_binding.goal_sequence
        or previous_manifest.authority_generation
        != current_binding.goal_sequence - 1
        or receipt.goal_sequence != expected_previous.goal_sequence
        or receipt.goal_id != expected_previous.goal_id
        or receipt.candidate_commit
        != current_binding.predecessor_completion_commit
        or receipt.authority_generation != previous_manifest.authority_generation
        or receipt.authority_policy_sha256 != previous_anchored.sha256
        or receipt.authority_anchor_commit != previous_anchored.anchor_commit
        or receipt.authority_anchor_receipt_sha256
        != registered_anchor.receipt_sha256
        or receipt.remote_ref
        != candidate_freeze_ref(
            previous_manifest,
            receipt.goal_sequence,
            receipt.candidate_commit,
        )
        or receipt.remote_subject != receipt.candidate_commit
        or receipt.remote_tree != receipt.candidate_tree
    ):
        raise SealedBlindCustodyError(
            "previous Goal PASS disagrees with the immutable transition"
        )
    previous_binding = load_current_goal_binding(
        repository_root,
        receipt.candidate_commit,
        previous_manifest,
    )
    _previous_state, previous_document_sha256 = load_current_goal_document_state(
        repository_root,
        receipt.candidate_commit,
        previous_manifest,
        previous_binding,
    )
    if (
        previous_binding.goal_id != receipt.goal_id
        or previous_binding.goal_sequence != receipt.goal_sequence
        or previous_binding.predecessor_goal_id != receipt.predecessor_goal_id
        or previous_binding.predecessor_completion_commit
        != receipt.predecessor_completion_commit
        or previous_binding.automated_gate_contract_sha256
        != receipt.automated_gate_contract_sha256
        or receipt.current_goal_document_sha256 != previous_document_sha256
        or git_blob_sha256(
            repository_root,
            receipt.candidate_commit,
            previous_manifest.current_goal_binding_path,
        )
        != receipt.current_goal_binding_sha256
        or _git(
            repository_root,
            "show",
            "-s",
            "--format=%T",
            receipt.candidate_commit,
        )
        != receipt.candidate_tree
    ):
        raise SealedBlindCustodyError(
            "previous Goal PASS cannot be reproduced from its candidate commit"
        )
    _require_remote_freeze_ref(
        repository_root=repository_root,
        remote_ref=receipt.remote_ref,
        candidate_commit=receipt.candidate_commit,
    )
    return RegisteredGoalGatePass(receipt=receipt, receipt_sha256=observed_sha256)


def register_goal_gate_pass(
    *,
    registry_path: Path,
    repository_root: Path,
    receipt: AgentGatePassReceipt,
    materialized_receipt_path: Path,
) -> RegisteredGoalGatePass:
    anchored = load_anchored_authority_policy(repository_root, receipt.candidate_commit)
    manifest = anchored.manifest
    require_formal_origin(repository_root, manifest)
    binding = load_current_goal_binding(
        repository_root,
        receipt.candidate_commit,
        manifest,
    )
    _goal_state, goal_document_sha256 = load_current_goal_document_state(
        repository_root,
        receipt.candidate_commit,
        manifest,
        binding,
    )
    require_predecessor_goal_pass(
        registry_path=registry_path,
        repository_root=repository_root,
        anchored_policy=anchored,
        current_binding=binding,
    )
    verify_payload_signature(
        payload=unsigned_payload(receipt),
        signature=receipt.authority_signature,
        manifest=manifest,
        expected_role="FINAL_GATE",
    )
    registered_anchor = load_registered_authority_anchor(
        registry_path=registry_path,
        repository_root=repository_root,
        manifest=manifest,
    )
    binding_sha256 = git_blob_sha256(
        repository_root,
        receipt.candidate_commit,
        manifest.current_goal_binding_path,
    )
    if (
        receipt.goal_sequence != binding.goal_sequence
        or receipt.goal_id != binding.goal_id
        or receipt.predecessor_goal_id != binding.predecessor_goal_id
        or receipt.predecessor_completion_commit
        != binding.predecessor_completion_commit
        or receipt.current_goal_binding_sha256 != binding_sha256
        or receipt.current_goal_document_sha256 != goal_document_sha256
        or receipt.automated_gate_contract_sha256
        != binding.automated_gate_contract_sha256
        or receipt.authority_policy_sha256 != anchored.sha256
        or receipt.authority_anchor_commit != anchored.anchor_commit
        or receipt.authority_anchor_receipt_sha256
        != registered_anchor.receipt_sha256
        or receipt.remote_ref
        != candidate_freeze_ref(
            manifest,
            receipt.goal_sequence,
            receipt.candidate_commit,
        )
        or receipt.remote_subject != receipt.candidate_commit
        or receipt.remote_tree != receipt.candidate_tree
        or _git(
            repository_root,
            "show",
            "-s",
            "--format=%T",
            receipt.candidate_commit,
        )
        != receipt.candidate_tree
    ):
        raise SealedBlindCustodyError(
            "AGENT_GATE_PASS disagrees with candidate Git or Program authority"
        )

    _require_remote_freeze_ref(
        repository_root=repository_root,
        remote_ref=receipt.remote_ref,
        candidate_commit=receipt.candidate_commit,
    )

    content = _serialize_pass(receipt)
    materialized = read_external_snapshot(
        materialized_receipt_path,
        repository_root,
    )
    if materialized.content != content:
        raise SealedBlindCustodyError(
            "materialized AGENT_GATE_PASS bytes disagree with the signed receipt"
        )
    receipt_sha256 = hashlib.sha256(content).hexdigest()
    registry = require_external_existing(registry_path, repository_root)
    with _registry_transaction(registry, immediate=True) as connection:
        identity, path_sha256 = _read_registry_metadata(connection, registry)
        _assert_registry_matches_policy(
            identity=identity,
            path_sha256=path_sha256,
            manifest=manifest,
        )
        existing = connection.execute(
            """
            SELECT pass_receipt_bytes, pass_receipt_sha256
            FROM goal_gate_passes WHERE goal_sequence = ?
            """,
            (receipt.goal_sequence,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO goal_gate_passes (
                    goal_sequence, goal_id, candidate_commit, candidate_tree,
                    pass_receipt_bytes, pass_receipt_sha256,
                    canonical_candidate_ref, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.goal_sequence,
                    receipt.goal_id,
                    receipt.candidate_commit,
                    receipt.candidate_tree,
                    content,
                    receipt_sha256,
                    receipt.remote_ref,
                    _utc_now_text(),
                ),
            )
        else:
            existing_content = bytes(existing[0])
            existing_sha256 = hashlib.sha256(existing_content).hexdigest()
            if (
                existing_sha256 != str(existing[1])
                or existing_content != content
                or existing_sha256 != receipt_sha256
            ):
                raise SealedBlindCustodyError(
                    "Goal sequence is already registered to a different PASS"
                )
        _require_remote_freeze_ref(
            repository_root=repository_root,
            remote_ref=receipt.remote_ref,
            candidate_commit=receipt.candidate_commit,
        )
    return RegisteredGoalGatePass(receipt=receipt, receipt_sha256=receipt_sha256)


def recover_registered_goal_gate_pass(
    *,
    registry_path: Path,
    repository_root: Path,
    goal_sequence: int,
    output_path: Path,
) -> dict[str, str]:
    registry = require_external_existing(registry_path, repository_root)
    with _registry_transaction(registry) as connection:
        row = connection.execute(
            """
            SELECT pass_receipt_bytes, pass_receipt_sha256
            FROM goal_gate_passes WHERE goal_sequence = ?
            """,
            (goal_sequence,),
        ).fetchone()
    if row is None:
        raise SealedBlindCustodyError("Goal PASS is not registered")
    content = bytes(row[0])
    receipt_sha256 = hashlib.sha256(content).hexdigest()
    if receipt_sha256 != str(row[1]):
        raise SealedBlindCustodyError("registered Goal PASS hash mismatch")
    try:
        receipt = AgentGatePassReceipt.model_validate_json(content)
    except ValueError as exc:
        raise SealedBlindCustodyError("registered Goal PASS is invalid") from exc
    anchored = load_anchored_authority_policy(
        repository_root,
        receipt.candidate_commit,
    )
    verify_payload_signature(
        payload=unsigned_payload(receipt),
        signature=receipt.authority_signature,
        manifest=anchored.manifest,
        expected_role="FINAL_GATE",
    )
    if output_path.exists():
        existing = read_external_snapshot(output_path, repository_root)
        if existing.content != content:
            raise SealedBlindCustodyError(
                "existing recovered Goal PASS path contains different bytes"
            )
    else:
        require_external_target(output_path, repository_root)
        write_external_bytes_exclusive(output_path, content, repository_root)
    return {
        "goal_id": receipt.goal_id,
        "candidate_commit": receipt.candidate_commit,
        "receipt_sha256": receipt_sha256,
        "output_path": str(output_path.resolve(strict=True)),
    }


def load_registered_authority_anchor(
    *,
    registry_path: Path,
    repository_root: Path,
    manifest: AgentGateAuthorityManifest,
) -> RegisteredAuthorityAnchor:
    registry = require_external_existing(registry_path, repository_root)
    with _registry_transaction(registry) as connection:
        identity, path_sha256 = _read_registry_metadata(connection, registry)
        _assert_registry_matches_policy(
            identity=identity,
            path_sha256=path_sha256,
            manifest=manifest,
        )
        row = connection.execute(
            """
            SELECT anchor_receipt_bytes, anchor_receipt_sha256
            FROM authority_anchors WHERE authority_generation = ?
            """,
            (manifest.authority_generation,),
        ).fetchone()
    if row is None:
        raise SealedBlindCustodyError("authority generation is not externally anchored")
    content = bytes(row[0])
    observed_sha256 = hashlib.sha256(content).hexdigest()
    if observed_sha256 != str(row[1]):
        raise SealedBlindCustodyError("stored authority anchor receipt hash mismatch")
    try:
        receipt = AuthorityAnchorReceipt.model_validate_json(content)
    except ValueError as exc:
        raise SealedBlindCustodyError("stored authority anchor receipt is invalid") from exc
    verify_payload_signature(
        payload=unsigned_payload(receipt),
        signature=receipt.authority_signature,
        manifest=manifest,
        expected_role="SEALED_CUSTODY",
    )
    expected_program_core_sha256 = compute_git_blob_bundle_hash(
        repository_root,
        receipt.anchor_commit,
        manifest.program_core_paths,
    )
    if receipt.program_core_sha256 != expected_program_core_sha256:
        raise SealedBlindCustodyError(
            "stored authority anchor Program core hash mismatch"
        )
    return RegisteredAuthorityAnchor(
        receipt=receipt,
        receipt_sha256=observed_sha256,
    )


def _read_registry_metadata(
    connection: sqlite3.Connection,
    registry_path: Path,
) -> tuple[str, str]:
    row = connection.execute(
        """
        SELECT registry_identity_sha256, canonical_path_sha256
        FROM custody_metadata WHERE singleton = 1
        """
    ).fetchone()
    if row is None:
        raise SealedBlindCustodyError("custody registry metadata is missing")
    identity, path_sha256 = str(row[0]), str(row[1])
    if path_sha256 != canonical_path_sha256(registry_path):
        raise SealedBlindCustodyError("custody registry was copied to a non-canonical path")
    return identity, path_sha256


def _assert_registry_matches_policy(
    *,
    identity: str,
    path_sha256: str,
    manifest: AgentGateAuthorityManifest,
) -> None:
    if identity != manifest.custody_registry_identity_sha256:
        raise SealedBlindCustodyError("custody registry identity is not pinned by policy")
    if path_sha256 != manifest.custody_registry_path_sha256:
        raise SealedBlindCustodyError("custody registry path is not pinned by policy")


def mint_sealed_blind_run(
    *,
    registry_path: Path,
    mint_receipt_path: Path,
    repository_root: Path,
    manifest: AgentGateAuthorityManifest,
    authority_policy_sha256: str,
    receipt: SealedAgentBlindMintReceipt,
) -> dict[str, object]:
    require_scoped_goal(manifest, receipt.goal_id)
    if receipt.authority_policy_sha256 != authority_policy_sha256:
        raise SealedBlindCustodyError("sealed mint authority policy mismatch")
    try:
        verify_payload_signature(
            payload=unsigned_payload(receipt),
            signature=receipt.authority_signature,
            manifest=manifest,
            expected_role="SEALED_CUSTODY",
        )
    except ValueError as exc:
        raise SealedBlindCustodyError("sealed mint signature is invalid") from exc
    registry = require_external_existing(registry_path, repository_root)
    output = require_external_target(mint_receipt_path, repository_root)
    receipt_bytes = _serialize(receipt)
    mint_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    with _registry_transaction(registry, immediate=True) as connection:
        identity, path_sha256 = _read_registry_metadata(connection, registry)
        _assert_registry_matches_policy(
            identity=identity,
            path_sha256=path_sha256,
            manifest=manifest,
        )
        if (
            receipt.custody_registry_identity_sha256 != identity
            or receipt.custody_registry_path_sha256 != path_sha256
        ):
            raise SealedBlindCustodyError(
                "sealed mint receipt does not bind the custody registry"
            )
        existing = connection.execute(
            """
            SELECT mint_receipt_bytes, mint_receipt_sha256
            FROM sealed_runs
            WHERE goal_id = ? AND candidate_commit = ?
              AND tranche_commitment_sha256 = ?
            """,
            (
                receipt.goal_id,
                receipt.candidate_commit,
                receipt.tranche_commitment_sha256,
            ),
        ).fetchone()
        if existing is not None:
            existing_bytes = bytes(existing[0])
            if (
                existing_bytes != receipt_bytes
                or hashlib.sha256(existing_bytes).hexdigest() != str(existing[1])
            ):
                raise SealedBlindCustodyError(
                    "existing mint binding differs from signed retry"
                )
        else:
            next_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(mint_sequence), 0) + 1 FROM sealed_runs"
                ).fetchone()[0]
            )
            if receipt.mint_sequence != next_sequence:
                raise SealedBlindCustodyError(
                    "signed mint sequence is not the next custody sequence"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO sealed_runs (
                        mint_sequence, goal_id, candidate_commit, candidate_tree,
                        tranche_commitment_sha256, one_shot_nonce_sha256,
                        custodian_task_id, authority_policy_sha256,
                        mint_receipt_bytes, mint_receipt_sha256, state, minted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'MINTED', ?)
                    """,
                    (
                        receipt.mint_sequence,
                        receipt.goal_id,
                        receipt.candidate_commit,
                        receipt.candidate_tree,
                        receipt.tranche_commitment_sha256,
                        receipt.one_shot_nonce_sha256,
                        receipt.custodian_task_id,
                        receipt.authority_policy_sha256,
                        receipt_bytes,
                        mint_sha256,
                        receipt.minted_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SealedBlindCustodyError(
                    "candidate tranche or one-shot nonce was already minted"
                ) from exc
    write_external_bytes_exclusive(output, receipt_bytes, repository_root)
    return {
        "registry_identity_sha256": manifest.custody_registry_identity_sha256,
        "mint_receipt_sha256": mint_sha256,
        "mint_receipt_path": str(output),
    }


def consume_minted_run(
    *,
    registry_path: Path,
    mint_receipt_path: Path,
    attempt_commitment_sha256: str,
    repository_root: Path,
    manifest: AgentGateAuthorityManifest,
    authority_policy_sha256: str,
) -> ConsumedSealedRun:
    if len(attempt_commitment_sha256) != 64 or any(
        character not in "0123456789abcdef"
        for character in attempt_commitment_sha256
    ):
        raise SealedBlindCustodyError("sealed blind attempt commitment is invalid")
    registry = require_external_existing(registry_path, repository_root)
    mint_snapshot = read_external_snapshot(mint_receipt_path, repository_root)
    try:
        mint = SealedAgentBlindMintReceipt.model_validate_json(mint_snapshot.content)
    except ValueError as exc:
        raise SealedBlindCustodyError(f"invalid sealed blind mint receipt: {exc}") from exc
    if mint.authority_policy_sha256 != authority_policy_sha256:
        raise SealedBlindCustodyError("sealed blind mint authority policy mismatch")
    require_scoped_goal(manifest, mint.goal_id)
    verify_payload_signature(
        payload=unsigned_payload(mint),
        signature=mint.authority_signature,
        manifest=manifest,
        expected_role="SEALED_CUSTODY",
    )
    if mint.custody_registry_identity_sha256 != manifest.custody_registry_identity_sha256:
        raise SealedBlindCustodyError("sealed blind mint registry identity mismatch")
    if mint.custody_registry_path_sha256 != manifest.custody_registry_path_sha256:
        raise SealedBlindCustodyError("sealed blind mint registry path mismatch")

    with _registry_transaction(registry, immediate=True) as connection:
        identity, path_sha256 = _read_registry_metadata(connection, registry)
        _assert_registry_matches_policy(identity=identity, path_sha256=path_sha256, manifest=manifest)
        cursor = connection.execute(
            """
            UPDATE sealed_runs
            SET state = 'CONSUMED', consumed_at = ?, attempt_commitment_sha256 = ?
            WHERE mint_sequence = ? AND goal_id = ? AND candidate_commit = ?
              AND candidate_tree = ? AND tranche_commitment_sha256 = ?
              AND one_shot_nonce_sha256 = ? AND custodian_task_id = ?
              AND authority_policy_sha256 = ? AND mint_receipt_sha256 = ?
              AND state = 'MINTED'
            """,
            (
                _utc_now_text(),
                attempt_commitment_sha256,
                mint.mint_sequence,
                mint.goal_id,
                mint.candidate_commit,
                mint.candidate_tree,
                mint.tranche_commitment_sha256,
                mint.one_shot_nonce_sha256,
                mint.custodian_task_id,
                mint.authority_policy_sha256,
                mint_snapshot.sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise SealedBlindCustodyError(
                "sealed blind run was not pre-minted or was already consumed"
            )
    return ConsumedSealedRun(
        mint=mint,
        mint_snapshot=mint_snapshot,
        attempt_commitment_sha256=attempt_commitment_sha256,
    )


def complete_scored_run(
    *,
    registry_path: Path,
    repository_root: Path,
    manifest: AgentGateAuthorityManifest,
    mint: SealedAgentBlindMintReceipt,
    attempt_commitment_sha256: str,
    score_input_manifest_sha256: str,
    score_receipt_sha256: str,
) -> None:
    registry = require_external_existing(registry_path, repository_root)
    with _registry_transaction(registry, immediate=True) as connection:
        identity, path_sha256 = _read_registry_metadata(connection, registry)
        _assert_registry_matches_policy(identity=identity, path_sha256=path_sha256, manifest=manifest)
        cursor = connection.execute(
            """
            UPDATE sealed_runs
            SET state = 'COMPLETED', completed_at = ?,
                score_input_manifest_sha256 = ?, score_receipt_sha256 = ?
            WHERE mint_sequence = ? AND one_shot_nonce_sha256 = ?
              AND attempt_commitment_sha256 = ? AND state = 'CONSUMED'
            """,
            (
                _utc_now_text(),
                score_input_manifest_sha256,
                score_receipt_sha256,
                mint.mint_sequence,
                mint.one_shot_nonce_sha256,
                attempt_commitment_sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise SealedBlindCustodyError("sealed blind score completion is not unique")


def claim_attempt_receipt(
    *,
    registry_path: Path,
    repository_root: Path,
    manifest: AgentGateAuthorityManifest,
    mint: SealedAgentBlindMintReceipt,
    attempt_commitment_sha256: str,
    attempt_receipt_sha256: str,
) -> None:
    registry = require_external_existing(registry_path, repository_root)
    with _registry_transaction(registry, immediate=True) as connection:
        identity, path_sha256 = _read_registry_metadata(connection, registry)
        _assert_registry_matches_policy(
            identity=identity,
            path_sha256=path_sha256,
            manifest=manifest,
        )
        cursor = connection.execute(
            """
            UPDATE sealed_runs
            SET attempt_receipt_sha256 = ?
            WHERE mint_sequence = ? AND one_shot_nonce_sha256 = ?
              AND attempt_commitment_sha256 = ?
              AND score_input_manifest_sha256 IS NOT NULL
              AND score_receipt_sha256 IS NOT NULL
              AND attempt_receipt_sha256 IS NULL
              AND state = 'COMPLETED'
            """,
            (
                attempt_receipt_sha256,
                mint.mint_sequence,
                mint.one_shot_nonce_sha256,
                attempt_commitment_sha256,
            ),
        )
        if cursor.rowcount != 1:
            existing = connection.execute(
                """
                SELECT attempt_receipt_sha256 FROM sealed_runs
                WHERE mint_sequence = ? AND one_shot_nonce_sha256 = ?
                  AND attempt_commitment_sha256 = ? AND state = 'COMPLETED'
                """,
                (
                    mint.mint_sequence,
                    mint.one_shot_nonce_sha256,
                    attempt_commitment_sha256,
                ),
            ).fetchone()
            if existing is None or str(existing[0]) != attempt_receipt_sha256:
                raise SealedBlindCustodyError(
                    "sealed blind attempt receipt was already claimed or not completed"
                )


def read_run_state(
    *,
    registry_path: Path,
    repository_root: Path,
    manifest: AgentGateAuthorityManifest,
    one_shot_nonce_sha256: str,
) -> dict[str, str | int | None]:
    registry = require_external_existing(registry_path, repository_root)
    with _registry_transaction(registry) as connection:
        identity, path_sha256 = _read_registry_metadata(connection, registry)
        _assert_registry_matches_policy(identity=identity, path_sha256=path_sha256, manifest=manifest)
        row = connection.execute(
            """
            SELECT mint_sequence, state, mint_receipt_sha256,
                   attempt_commitment_sha256, attempt_receipt_sha256,
                   score_input_manifest_sha256, score_receipt_sha256
            FROM sealed_runs WHERE one_shot_nonce_sha256 = ?
            """,
            (one_shot_nonce_sha256,),
        ).fetchone()
    if row is None:
        raise SealedBlindCustodyError("sealed blind nonce is absent from custody")
    return {
        "mint_sequence": int(row[0]),
        "state": str(row[1]),
        "mint_receipt_sha256": str(row[2]),
        "attempt_commitment_sha256": None if row[3] is None else str(row[3]),
        "attempt_receipt_sha256": None if row[4] is None else str(row[4]),
        "score_input_manifest_sha256": None if row[5] is None else str(row[5]),
        "score_receipt_sha256": None if row[6] is None else str(row[6]),
    }
