from __future__ import annotations

import hashlib
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_SOURCE_ENVELOPE_VERSION = b"\x01"
_NONCE_BYTES = 12
_HKDF_SALT = b"BreezeTravel TripUnderstanding source v1"
_HKDF_INFO = b"trip-understanding-v3-source-encryption"


class SourceCipher:
    """Encrypt recoverable source text without persisting the root secret.

    The source ID and content hash are authenticated as associated data so a
    ciphertext cannot be moved to another source row without detection.
    """

    def __init__(self, root_secret: str):
        if not root_secret:
            raise ValueError("source encryption requires a non-empty root secret")
        self._key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=_HKDF_INFO,
        ).derive(root_secret.encode("utf-8"))
        self.key_ref = f"tu3-source-v1:{hashlib.sha256(self._key).hexdigest()[:12]}"

    @staticmethod
    def _aad(source_id: str, content_hash: str, purpose: str) -> bytes:
        return f"{source_id}:{content_hash}:{purpose}".encode("utf-8")

    def encrypt(
        self,
        text: str,
        *,
        source_id: str,
        content_hash: str,
        purpose: str = "source",
    ) -> bytes:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(self._key).encrypt(
            nonce,
            text.encode("utf-8"),
            self._aad(source_id, content_hash, purpose),
        )
        return _SOURCE_ENVELOPE_VERSION + nonce + ciphertext

    def decrypt(
        self,
        envelope: bytes,
        *,
        source_id: str,
        content_hash: str,
        purpose: str = "source",
    ) -> str:
        if len(envelope) <= 1 + _NONCE_BYTES or envelope[:1] != _SOURCE_ENVELOPE_VERSION:
            raise ValueError("unsupported source encryption envelope")
        nonce = envelope[1 : 1 + _NONCE_BYTES]
        ciphertext = envelope[1 + _NONCE_BYTES :]
        plaintext = AESGCM(self._key).decrypt(
            nonce,
            ciphertext,
            self._aad(source_id, content_hash, purpose),
        )
        return plaintext.decode("utf-8")
