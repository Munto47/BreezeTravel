from __future__ import annotations

import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)
from pydantic import BaseModel

from evals.agent_gate_v1.contracts import (
    AgentGateAuthorityManifest,
    AuthorityPublicKey,
    AuthorityRole,
    DetachedAuthoritySignature,
)


class AuthoritySignatureError(ValueError):
    pass


SIGNATURE_DOMAIN = b"BREEZETRAVEL_AGENT_GATE_V1\0"


def canonical_payload_bytes(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def unsigned_payload(value: BaseModel | dict[str, object]) -> dict[str, object]:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else dict(value)
    )
    payload.pop("authority_signature", None)
    return payload


def _signature_message(payload: dict[str, object]) -> tuple[bytes, str]:
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        raise AuthoritySignatureError("signed payload requires a schema_version")
    encoded = canonical_payload_bytes(payload)
    payload_sha256 = hashlib.sha256(encoded).hexdigest()
    message = (
        SIGNATURE_DOMAIN
        + schema_version.encode("utf-8")
        + b"\0"
        + bytes.fromhex(payload_sha256)
    )
    return message, payload_sha256


def authority_for_role(
    manifest: AgentGateAuthorityManifest,
    role: AuthorityRole,
) -> AuthorityPublicKey:
    matches = [item for item in manifest.authorities if item.role == role]
    if len(matches) != 1:
        raise AuthoritySignatureError(f"authority role {role} is not uniquely pinned")
    return matches[0]


def verify_payload_signature(
    *,
    payload: dict[str, object],
    signature: DetachedAuthoritySignature,
    manifest: AgentGateAuthorityManifest,
    expected_role: AuthorityRole,
) -> None:
    authority = authority_for_role(manifest, expected_role)
    if (
        signature.authority_role != expected_role
        or signature.authority_id != authority.authority_id
    ):
        raise AuthoritySignatureError("artifact signature authority binding mismatch")
    message, payload_sha256 = _signature_message(payload)
    if payload_sha256 != signature.signed_payload_sha256:
        raise AuthoritySignatureError("artifact signed payload hash mismatch")
    public_bytes = base64.b64decode(authority.public_key_base64, validate=True)
    try:
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            base64.b64decode(signature.signature_base64, validate=True),
            message,
        )
    except (InvalidSignature, ValueError) as exc:
        raise AuthoritySignatureError("artifact authority signature is invalid") from exc
