"""密码哈希工具：PBKDF2-HMAC-SHA256（stdlib，不引入新依赖）。

存储格式与 Django 兼容：`pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>`
迭代次数 260000（2024 OWASP 推荐 PBKDF2-SHA256 600k；此处折中考虑后端 CPU 与延迟）。
"""

import hashlib
import hmac
import os

_ITERATIONS = 260_000
_HASH_NAME = "sha256"
_SALT_BYTES = 16
_DKLEN = 32


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("密码不能为空")
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(_HASH_NAME, password.encode("utf-8"), salt, _ITERATIONS, dklen=_DKLEN)
    return f"pbkdf2_{_HASH_NAME}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if not password or not encoded:
        return False
    try:
        scheme, iter_str, salt_hex, hash_hex = encoded.split("$", 3)
    except ValueError:
        return False
    if not scheme.startswith("pbkdf2_"):
        return False
    hash_name = scheme.removeprefix("pbkdf2_")
    try:
        iterations = int(iter_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac(hash_name, password.encode("utf-8"), salt, iterations, dklen=len(expected))
    return hmac.compare_digest(dk, expected)
