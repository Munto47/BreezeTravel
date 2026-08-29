"""Non-formal signing helpers for tests and pre-activation development only.

Formal Agent Gate processes must use the repository-external signer IPC and
must never import this module or receive a private-key path.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from evals.agent_gate_v1.contracts import (
    AuthorityPublicKey,
    AuthorityRole,
    DetachedAuthoritySignature,
)
from evals.agent_gate_v1.path_security import (
    read_external_snapshot,
    write_external_bytes_exclusive,
)
from evals.agent_gate_v1.signing import AuthoritySignatureError, _signature_message


def generate_authority_keypair(
    *,
    private_key_path: Path,
    repository_root: Path,
    role: AuthorityRole,
    authority_id: str,
    task_id: str,
) -> AuthorityPublicKey:
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    write_external_bytes_exclusive(
        private_key_path,
        private_bytes,
        repository_root,
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return AuthorityPublicKey.model_validate(
        {
            "role": role,
            "authority_id": authority_id,
            "algorithm": "ED25519",
            "public_key_base64": base64.b64encode(public_bytes).decode("ascii"),
            "public_key_sha256": hashlib.sha256(public_bytes).hexdigest(),
            "generated_by_task_id": task_id,
            "private_key_storage": "REPOSITORY_EXTERNAL",
            "human_evidence": False,
        }
    )


def sign_payload_for_development(
    *,
    payload: dict[str, object],
    private_key_path: Path,
    repository_root: Path,
    authority: AuthorityPublicKey,
) -> DetachedAuthoritySignature:
    private_snapshot = read_external_snapshot(private_key_path, repository_root)
    if len(private_snapshot.content) != 32:
        raise AuthoritySignatureError("authority private key must be a raw Ed25519 key")
    private_key = Ed25519PrivateKey.from_private_bytes(private_snapshot.content)
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if hashlib.sha256(public_bytes).hexdigest() != authority.public_key_sha256:
        raise AuthoritySignatureError("private key does not match the pinned authority")
    message, payload_sha256 = _signature_message(payload)
    return DetachedAuthoritySignature.model_validate(
        {
            "authority_role": authority.role,
            "authority_id": authority.authority_id,
            "algorithm": "ED25519",
            "signed_payload_sha256": payload_sha256,
            "signature_base64": base64.b64encode(private_key.sign(message)).decode(
                "ascii"
            ),
        }
    )
