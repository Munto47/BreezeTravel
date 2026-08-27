from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


COOKIE_VERSION = "v1"


def _signature(token: str, signing_key: str) -> str:
    digest = hmac.new(signing_key.encode("utf-8"), token.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def mint_capability(signing_key: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    cookie = f"{COOKIE_VERSION}.{token}.{_signature(token, signing_key)}"
    return cookie, hashlib.sha256(token.encode("ascii")).hexdigest()


def capability_hash(cookie_value: str | None, signing_key: str) -> str | None:
    if not cookie_value:
        return None
    parts = cookie_value.split(".")
    if len(parts) != 3 or parts[0] != COOKIE_VERSION:
        return None
    token, supplied_signature = parts[1], parts[2]
    if not token or not hmac.compare_digest(supplied_signature, _signature(token, signing_key)):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()
