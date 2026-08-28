from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from evals.agent_gate_v1.contracts import (
    AuthorityActivationReadinessReceipt,
    AgentGateAuthorityManifest,
    CurrentGoalBinding,
    CurrentGoalDocumentState,
)
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1.signing import unsigned_payload, verify_payload_signature


AUTHORITY_POLICY_PATH = "backend/eval_data/agent_gate_v1/authority_policy.json"
CANONICAL_ORIGIN = "https://github.com/Munto47/BreezeTravel.git"
LEGACY_BASELINE_COMMIT = "7bdd1a6abd9c10c6076aca67f08de785027501a0"


class AuthorityPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class AnchoredAuthorityPolicy:
    manifest: AgentGateAuthorityManifest
    content: bytes
    sha256: str
    anchor_commit: str
    candidate_commit: str


def _stable_program_facts(manifest: AgentGateAuthorityManifest) -> str:
    value = manifest.model_dump(
        mode="json",
        exclude={"authority_generation", "authority_phase", "frozen_at"},
    )
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _git(root: Path, *args: str, text: bool = True):
    result = subprocess.run(
        [trusted_host_tool("git"), "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=text,
    )
    if result.returncode != 0:
        raise AuthorityPolicyError(f"Git authority readback failed: {' '.join(args)}")
    return result.stdout.strip() if text else result.stdout


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        [
            trusted_host_tool("git"),
            "-C",
            str(root),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def load_anchored_authority_policy(
    repository_root: Path,
    candidate_commit: str,
) -> AnchoredAuthorityPolicy:
    root = repository_root.resolve(strict=True)
    candidate_content = _git(
        root,
        "show",
        f"{candidate_commit}:{AUTHORITY_POLICY_PATH}",
        text=False,
    )
    try:
        manifest = AgentGateAuthorityManifest.model_validate_json(candidate_content)
    except ValueError as exc:
        raise AuthorityPolicyError(f"invalid candidate authority policy: {exc}") from exc
    if manifest.legacy_baseline_commit != LEGACY_BASELINE_COMMIT:
        raise AuthorityPolicyError("authority policy legacy baseline is not pinned")
    if AUTHORITY_POLICY_PATH not in manifest.config_roots:
        raise AuthorityPolicyError("authority policy must include itself in config bindings")
    if not _is_ancestor(root, LEGACY_BASELINE_COMMIT, candidate_commit):
        raise AuthorityPolicyError("candidate is not descended from the governed baseline")
    if manifest.authority_phase != "ACTIVE":
        raise AuthorityPolicyError(
            "authority bootstrap cannot produce Gate evidence or register an anchor"
        )

    history = _git(
        root,
        "log",
        "--format=%H",
        candidate_commit,
        "--",
        AUTHORITY_POLICY_PATH,
    ).splitlines()
    if not history:
        raise AuthorityPolicyError("authority policy has no Goal-generation anchor commit")
    anchor_commit = history[0]
    if not _is_ancestor(root, anchor_commit, candidate_commit):
        raise AuthorityPolicyError("authority anchor is not an ancestor of the candidate")
    anchor_content = _git(
        root,
        "show",
        f"{anchor_commit}:{AUTHORITY_POLICY_PATH}",
        text=False,
    )
    if anchor_content != candidate_content:
        raise AuthorityPolicyError("authority policy changed inside its Goal generation")
    try:
        binding = CurrentGoalBinding.model_validate_json(
            _git(
                root,
                "show",
                f"{anchor_commit}:{manifest.current_goal_binding_path}",
                text=False,
            )
        )
    except ValueError as exc:
        raise AuthorityPolicyError("authority anchor has no valid current Goal binding") from exc
    if (
        binding.goal_sequence != manifest.authority_generation
        or binding.goal_id
        != manifest.goal_bindings[manifest.authority_generation - 1].goal_id
    ):
        raise AuthorityPolicyError(
            "authority generation is not anchored to its matching active Goal"
        )
    parent = _git(root, "rev-parse", f"{anchor_commit}^")
    previous_result = subprocess.run(
        [
            trusted_host_tool("git"),
            "-C",
            str(root),
            "show",
            f"{parent}:{AUTHORITY_POLICY_PATH}",
        ],
        check=False,
        capture_output=True,
    )
    if manifest.authority_generation == 1:
        if len(history) != 2 or previous_result.returncode != 0:
            raise AuthorityPolicyError(
                "active authority generation 1 requires exactly one bootstrap policy"
            )
        bootstrap_commit = history[-1]
        bootstrap_parent = _git(root, "rev-parse", f"{bootstrap_commit}^")
        bootstrap_predecessor = subprocess.run(
            [
                trusted_host_tool("git"),
                "-C",
                str(root),
                "show",
                f"{bootstrap_parent}:{AUTHORITY_POLICY_PATH}",
            ],
            check=False,
            capture_output=True,
        )
        if bootstrap_predecessor.returncode == 0:
            raise AuthorityPolicyError("authority bootstrap must be the policy's first Git addition")
        try:
            bootstrap = AgentGateAuthorityManifest.model_validate_json(
                _git(
                    root,
                    "show",
                    f"{bootstrap_commit}:{AUTHORITY_POLICY_PATH}",
                    text=False,
                )
            )
        except ValueError as exc:
            raise AuthorityPolicyError("authority bootstrap policy is invalid") from exc
        if (
            bootstrap.authority_phase != "BOOTSTRAP"
            or bootstrap.authority_generation != 1
            or _stable_program_facts(bootstrap) != _stable_program_facts(manifest)
            or manifest.frozen_at <= bootstrap.frozen_at
        ):
            raise AuthorityPolicyError(
                "authority activation changed stable bootstrap Program facts"
            )
        for path in manifest.bootstrap_core_paths:
            bootstrap_blob = _git(root, "show", f"{bootstrap_commit}:{path}", text=False)
            anchor_blob = _git(root, "show", f"{anchor_commit}:{path}", text=False)
            if bootstrap_blob != anchor_blob:
                raise AuthorityPolicyError(
                    f"bootstrap trust core changed before authority activation: {path}"
                )
        _validate_generation_one_activation(
            root=root,
            manifest=manifest,
            active_policy_content=candidate_content,
            anchor_commit=anchor_commit,
            bootstrap_commit=bootstrap_commit,
            bootstrap=bootstrap,
        )
    else:
        if previous_result.returncode != 0:
            raise AuthorityPolicyError("authority generation predecessor is absent")
        try:
            previous = AgentGateAuthorityManifest.model_validate_json(
                previous_result.stdout
            )
        except ValueError as exc:
            raise AuthorityPolicyError("previous authority generation is invalid") from exc
        if (
            previous.authority_generation + 1 != manifest.authority_generation
            or previous.authority_phase != "ACTIVE"
            or _stable_program_facts(previous) != _stable_program_facts(manifest)
            or manifest.frozen_at <= previous.frozen_at
        ):
            raise AuthorityPolicyError(
                "authority generation did not advance exactly once over stable Program facts"
            )
        for path in manifest.program_core_paths:
            previous_blob = _git(root, "show", f"{parent}:{path}", text=False)
            anchor_blob = _git(root, "show", f"{anchor_commit}:{path}", text=False)
            if previous_blob != anchor_blob:
                raise AuthorityPolicyError(
                    f"Program trust core changed across authority generations: {path}"
                )
    for path in manifest.immutable_protocol_paths:
        anchor_blob = _git(root, "show", f"{anchor_commit}:{path}", text=False)
        candidate_blob = _git(root, "show", f"{candidate_commit}:{path}", text=False)
        if anchor_blob != candidate_blob:
            raise AuthorityPolicyError(
                f"immutable Agent Gate protocol changed after its anchor: {path}"
            )
    _validate_contract_code_bindings(root, candidate_commit)
    return AnchoredAuthorityPolicy(
        manifest=manifest,
        content=candidate_content,
        sha256=hashlib.sha256(candidate_content).hexdigest(),
        anchor_commit=anchor_commit,
        candidate_commit=candidate_commit,
    )


def _validate_generation_one_activation(
    *,
    root: Path,
    manifest: AgentGateAuthorityManifest,
    active_policy_content: bytes,
    anchor_commit: str,
    bootstrap_commit: str,
    bootstrap: AgentGateAuthorityManifest,
) -> None:
    """Require a custody-signed, byte-bound readiness proof before G01 activation."""

    try:
        receipt_content = _git(
            root,
            "show",
            f"{anchor_commit}:{manifest.authority_activation_receipt_path}",
            text=False,
        )
        receipt = AuthorityActivationReadinessReceipt.model_validate_json(
            receipt_content
        )
    except (AuthorityPolicyError, ValueError) as exc:
        raise AuthorityPolicyError(
            "active authority generation 1 requires a valid activation readiness receipt"
        ) from exc

    bootstrap_policy_content = _git(
        root,
        "show",
        f"{bootstrap_commit}:{AUTHORITY_POLICY_PATH}",
        text=False,
    )
    expected = (
        manifest.policy_id,
        manifest.authority_generation,
        manifest.scope_goal_ids[0],
        bootstrap_commit,
        _git(root, "show", "-s", "--format=%T", bootstrap_commit),
        hashlib.sha256(bootstrap_policy_content).hexdigest(),
        hashlib.sha256(active_policy_content).hexdigest(),
        compute_git_blob_bundle_hash(
            root,
            bootstrap_commit,
            manifest.bootstrap_core_paths,
        ),
        compute_git_tree_bundle_hash_excluding(
            root,
            anchor_commit,
            {manifest.authority_activation_receipt_path},
        ),
        compute_git_blob_bundle_hash(
            root,
            anchor_commit,
            manifest.program_core_paths,
        ),
        compute_git_blob_bundle_hash(
            root,
            anchor_commit,
            manifest.config_roots,
        ),
        compute_git_blob_bundle_hash(
            root,
            anchor_commit,
            manifest.data_roots,
            excluded_paths={manifest.authority_activation_receipt_path},
        ),
    )
    observed = (
        receipt.policy_id,
        receipt.authority_generation,
        receipt.goal_id,
        receipt.bootstrap_commit,
        receipt.bootstrap_tree,
        receipt.bootstrap_policy_sha256,
        receipt.active_policy_sha256,
        receipt.bootstrap_core_sha256,
        receipt.active_tree_without_receipt_sha256,
        receipt.active_program_core_sha256,
        receipt.active_config_sha256,
        receipt.active_data_sha256,
    )
    if observed != expected:
        raise AuthorityPolicyError(
            "authority activation readiness does not bind the bootstrap and active policy"
        )
    registry_contract_sha256 = hashlib.sha256(
        _git(
            root,
            "show",
            (
                f"{anchor_commit}:backend/eval_data/agent_gate_v1/"
                "live_evidence_registry_contract.sql"
            ),
            text=False,
        )
    ).hexdigest()
    if receipt.live_capture.registry_contract_sha256 != registry_contract_sha256:
        raise AuthorityPolicyError(
            "authority activation live registry contract binding is invalid"
        )
    if not (bootstrap.frozen_at < receipt.created_at <= manifest.frozen_at):
        raise AuthorityPolicyError(
            "authority activation readiness timestamp is outside its policy transition"
        )
    try:
        verify_payload_signature(
            payload=unsigned_payload(receipt),
            signature=receipt.authority_signature,
            manifest=manifest,
            expected_role="SEALED_CUSTODY",
        )
    except ValueError as exc:
        raise AuthorityPolicyError(
            "authority activation readiness signature is invalid"
        ) from exc


def _validate_contract_code_bindings(repository_root: Path, candidate_commit: str) -> None:
    contracts = (
        (
            "backend/eval_data/agent_gate_v1/protocol_contract.json",
            "backend/evals/agent_gate_v1",
        ),
        (
            "backend/eval_data/trip_text_cards_agent_v2/agent_evaluation_contract.json",
            "backend/evals/trip_text_cards_agent_v2",
        ),
    )
    for contract_path, source_root in contracts:
        try:
            contract = json.loads(
                _git(
                    repository_root,
                    "show",
                    f"{candidate_commit}:{contract_path}",
                    text=False,
                )
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthorityPolicyError(
                f"invalid immutable protocol contract: {contract_path}"
            ) from exc
        bindings = contract.get("contract_code_sha256")
        if not isinstance(bindings, dict) or not bindings:
            raise AuthorityPolicyError(
                f"protocol contract has no executable byte bindings: {contract_path}"
            )
        for filename, expected_sha256 in bindings.items():
            if not isinstance(filename, str) or not isinstance(expected_sha256, str):
                raise AuthorityPolicyError("protocol code binding has an invalid type")
            content = _git(
                repository_root,
                "show",
                f"{candidate_commit}:{source_root}/{filename}",
                text=False,
            )
            if hashlib.sha256(content).hexdigest() != expected_sha256:
                raise AuthorityPolicyError(
                    f"protocol contract code hash mismatch: {source_root}/{filename}"
                )


def compute_git_blob_bundle_hash(
    repository_root: Path,
    candidate_commit: str,
    roots: list[str],
    *,
    excluded_paths: set[str] | None = None,
) -> str:
    root = repository_root.resolve(strict=True)
    exclusions = excluded_paths or set()
    if any(
        not path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
        for path in exclusions
    ):
        raise AuthorityPolicyError("blob bundle exclusions are invalid")
    records_by_path: dict[str, dict[str, str]] = {}
    for binding_root in sorted(roots):
        tree_lines = _git(
            root,
            "ls-tree",
            "-r",
            "--full-tree",
            candidate_commit,
            "--",
            binding_root,
        ).splitlines()
        if not tree_lines:
            raise AuthorityPolicyError(f"binding root is absent: {binding_root}")
        for tree_line in tree_lines:
            metadata, path = tree_line.split("\t", maxsplit=1)
            if path in exclusions:
                continue
            mode, object_type, blob_oid = metadata.split()
            if object_type != "blob":
                raise AuthorityPolicyError(f"binding subject is not a blob: {path}")
            content = _git(root, "show", f"{candidate_commit}:{path}", text=False)
            records_by_path[path] = {
                "path": path,
                "mode": mode,
                "git_blob_oid": blob_oid,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
    records = [records_by_path[path] for path in sorted(records_by_path)]
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_git_tree_bundle_hash_excluding(
    repository_root: Path,
    candidate_commit: str,
    excluded_paths: set[str],
) -> str:
    """Hash every candidate blob except explicitly self-referential receipt files."""

    root = repository_root.resolve(strict=True)
    if not excluded_paths or any(
        not path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
        for path in excluded_paths
    ):
        raise AuthorityPolicyError("tree bundle exclusions are invalid")
    lines = _git(
        root,
        "ls-tree",
        "-r",
        "--full-tree",
        candidate_commit,
    ).splitlines()
    if not lines:
        raise AuthorityPolicyError("candidate tree has no blobs")
    records: list[dict[str, str]] = []
    observed_exclusions: set[str] = set()
    for line in lines:
        metadata, path = line.split("\t", maxsplit=1)
        mode, object_type, blob_oid = metadata.split()
        if path in excluded_paths:
            observed_exclusions.add(path)
            continue
        if object_type != "blob":
            raise AuthorityPolicyError(f"candidate tree subject is not a blob: {path}")
        content = _git(root, "show", f"{candidate_commit}:{path}", text=False)
        records.append(
            {
                "path": path,
                "mode": mode,
                "git_blob_oid": blob_oid,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if observed_exclusions != excluded_paths:
        raise AuthorityPolicyError("activation receipt exclusion is absent from candidate")
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def git_blob_sha256(
    repository_root: Path,
    candidate_commit: str,
    repository_path: str,
) -> str:
    return hashlib.sha256(
        _git(
            repository_root,
            "show",
            f"{candidate_commit}:{repository_path}",
            text=False,
        )
    ).hexdigest()


def require_formal_origin(repository_root: Path, manifest: AgentGateAuthorityManifest) -> str:
    origin = _git(repository_root, "remote", "get-url", "origin")
    if origin != manifest.canonical_origin_url or origin != CANONICAL_ORIGIN:
        raise AuthorityPolicyError("formal Agent Gate requires the canonical HTTPS origin")
    return origin


def candidate_freeze_ref(
    manifest: AgentGateAuthorityManifest,
    goal_sequence: int,
    candidate_commit: str,
) -> str:
    if not 1 <= goal_sequence <= 7 or not re.fullmatch(r"[0-9a-f]{40}", candidate_commit):
        raise AuthorityPolicyError("invalid immutable candidate ref subject")
    return (
        f"{manifest.candidate_freeze_ref_prefix}"
        f"g{goal_sequence:02d}-{candidate_commit}"
    )


def require_scoped_goal(manifest: AgentGateAuthorityManifest, goal_id: str) -> None:
    if goal_id not in manifest.scope_goal_ids:
        raise AuthorityPolicyError("Goal is outside the immutable Agent Gate authority scope")


def compute_public_key_set_sha256(manifest: AgentGateAuthorityManifest) -> str:
    records = [
        {
            "role": authority.role,
            "authority_id": authority.authority_id,
            "public_key_sha256": authority.public_key_sha256,
        }
        for authority in sorted(manifest.authorities, key=lambda item: item.role)
    ]
    encoded = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_worktree_current_goal_binding(repository_root: Path) -> CurrentGoalBinding:
    """Read the active Goal profile without activating HARDENED authority.

    CORE Gate callers use this profile selector before any authority, signer,
    registry, or OCI code is imported. The candidate-bound loader below remains
    the stricter path used by HARDENED verification.
    """

    path = repository_root / "docs/governance/current_goal_binding.json"
    try:
        return CurrentGoalBinding.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise AuthorityPolicyError(f"invalid worktree Goal binding: {exc}") from exc


def load_candidate_current_goal_binding(
    repository_root: Path,
    candidate_commit: str,
) -> CurrentGoalBinding:
    """Read the candidate-bound Goal profile without activating authority."""

    path = "docs/governance/current_goal_binding.json"
    content = _git(repository_root, "show", f"{candidate_commit}:{path}", text=False)
    try:
        binding = CurrentGoalBinding.model_validate_json(content)
    except ValueError as exc:
        raise AuthorityPolicyError(f"invalid candidate Goal binding: {exc}") from exc
    if git_blob_sha256(
        repository_root,
        candidate_commit,
        binding.automated_gate_contract_path,
    ) != binding.automated_gate_contract_sha256:
        raise AuthorityPolicyError("candidate Goal automation contract hash mismatch")
    return binding


def load_current_goal_binding(
    repository_root: Path,
    candidate_commit: str,
    manifest: AgentGateAuthorityManifest,
) -> CurrentGoalBinding:
    content = _git(
        repository_root,
        "show",
        f"{candidate_commit}:{manifest.current_goal_binding_path}",
        text=False,
    )
    try:
        binding = CurrentGoalBinding.model_validate_json(content)
    except ValueError as exc:
        raise AuthorityPolicyError(f"invalid current Goal binding: {exc}") from exc
    require_scoped_goal(manifest, binding.goal_id)
    if binding.goal_sequence != manifest.authority_generation:
        raise AuthorityPolicyError(
            "current Goal must use its matching authority generation"
        )
    if binding.canonical_candidate_ref != manifest.canonical_candidate_ref:
        raise AuthorityPolicyError("current Goal and authority remote ref disagree")
    expected = manifest.goal_bindings[binding.goal_sequence - 1]
    expected_facts = (
        expected.goal_sequence,
        expected.goal_id,
        expected.predecessor_goal_id,
        expected.automated_gate_contract_path,
        expected.automated_gate_contract_sha256,
        expected.gate_profile,
    )
    observed_facts = (
        binding.goal_sequence,
        binding.goal_id,
        binding.predecessor_goal_id,
        binding.automated_gate_contract_path,
        binding.automated_gate_contract_sha256,
        binding.gate_profile,
    )
    if observed_facts != expected_facts:
        raise AuthorityPolicyError(
            "current Goal binding disagrees with the immutable Program transition table"
        )
    if binding.goal_sequence == 1 and (
        binding.predecessor_completion_commit
        != expected.initial_predecessor_completion_commit
    ):
        raise AuthorityPolicyError("G01 predecessor completion is not the pinned Blueprint")
    if git_blob_sha256(
        repository_root,
        candidate_commit,
        binding.automated_gate_contract_path,
    ) != binding.automated_gate_contract_sha256:
        raise AuthorityPolicyError("current Goal automation contract hash mismatch")
    if not _is_ancestor(
        repository_root,
        binding.predecessor_completion_commit,
        candidate_commit,
    ):
        raise AuthorityPolicyError("current Goal predecessor is not a candidate ancestor")
    return binding


_GOAL_STATE_PATTERN = re.compile(
    r"<!-- AGENT_GATE_CURRENT_GOAL_STATE\n(?P<payload>\{.*?\})\n-->",
    re.DOTALL,
)


def load_current_goal_document_state(
    repository_root: Path,
    candidate_commit: str,
    manifest: AgentGateAuthorityManifest,
    binding: CurrentGoalBinding,
) -> tuple[CurrentGoalDocumentState, str]:
    content = _git(
        repository_root,
        "show",
        f"{candidate_commit}:{manifest.current_goal_document_path}",
        text=False,
    )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthorityPolicyError("CURRENT_GOAL.md is not UTF-8") from exc
    matches = list(_GOAL_STATE_PATTERN.finditer(text))
    if len(matches) != 1:
        raise AuthorityPolicyError(
            "CURRENT_GOAL.md must contain exactly one machine Gate state"
        )
    try:
        state = CurrentGoalDocumentState.model_validate_json(
            matches[0].group("payload")
        )
    except ValueError as exc:
        raise AuthorityPolicyError(f"invalid CURRENT_GOAL.md Gate state: {exc}") from exc
    visible_goal_ids = re.findall(r"(?m)^Goal ID: ([A-Z0-9-]+)$", text)
    visible_statuses = re.findall(r"(?m)^Status: (APPROVED|IN_PROGRESS)$", text)
    visible_headings = re.findall(r"(?m)^# (APPROVED|IN_PROGRESS) GOAL：", text)
    if text.count("## Completion record") != 1:
        raise AuthorityPolicyError("CURRENT_GOAL.md has no unique Completion record")
    completion_parts = text.split("## Completion record", maxsplit=1)
    completion = completion_parts[1].split("\n## ", maxsplit=1)[0]
    visible_gate_lines = re.findall(
        r"(?m)^- Verification / Evidence / Gate result：`([^`\r\n]+)`；$",
        completion,
    )
    visible_external_status = re.findall(
        r"(?m)^- H1 / production / commercial：`([^`\r\n]+)`；$",
        completion,
    )
    archived_lines = re.findall(r"(?m)^- Goal archived：`(YES|NO)`；$", text)
    activated_lines = re.findall(r"(?m)^- Next activated：`(YES|NO)`；$", text)
    gate_tokens = (
        [part.strip() for part in visible_gate_lines[0].split("/")]
        if len(visible_gate_lines) == 1
        else []
    )
    forbidden_pre_pass_claims = {
        "AGENT_GATE_PASS",
        "TEXT_CARD_GATE_PASS",
        "LIVE_PROVIDER_PASS",
        "SEALED_AGENT_BLIND_PASS",
        "SEALED_BLIND_PASS",
        "HUMAN_USABILITY_PASS",
        "PRODUCTION_PASS",
        "COMMERCIAL_PASS",
    }
    expected_external_status = (
        f"{state.h1_status} / {state.production_status} / {state.commercial_status}"
    )
    visible_required = f"- Required gate：`{state.required_gate}`"
    visible_facts = (
        visible_goal_ids[0] if len(visible_goal_ids) == 1 else None,
        visible_statuses[0] if len(visible_statuses) == 1 else None,
        visible_headings[0] if len(visible_headings) == 1 else None,
    )
    expected_facts = (state.goal_id, state.goal_status, state.goal_status)
    if (
        state.goal_id != binding.goal_id
        or state.goal_status != binding.status
        or visible_facts != expected_facts
        or visible_required not in text
        or "- Status：`PENDING`；" not in completion
        or len(visible_gate_lines) != 1
        or state.gate_result not in gate_tokens
        or forbidden_pre_pass_claims.intersection(gate_tokens)
        or any(claim in completion for claim in forbidden_pre_pass_claims)
        or visible_external_status != [expected_external_status]
        or archived_lines != ["NO"]
        or activated_lines != ["NO"]
        or state.next_goal_id
        != (
            "TC-H1-G01-HUMAN-USABILITY"
            if binding.goal_sequence == 7
            else manifest.goal_bindings[binding.goal_sequence].goal_id
        )
    ):
        raise AuthorityPolicyError(
            "CURRENT_GOAL.md visible state disagrees with its active pre-PASS contract"
        )
    return state, hashlib.sha256(content).hexdigest()
