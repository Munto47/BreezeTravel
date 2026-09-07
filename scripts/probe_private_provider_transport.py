"""Twelve unauthenticated GET probes; never modifies the running service env.

Each child inherits the private launch env. The direct variant only adds the
target official hostname to that child's NO_PROXY value. No inference is sent.
Proxy URLs, secrets, response bodies and raw exception strings are not exported.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit
import urllib.request

import httpx


PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def proxy_presence():
    return {key: bool(value) for key, value in urllib.request.getproxies().items() if key in {"http", "https", "all", "no"}}


def failure(error):
    chain, cursor, seen = [], error, set()
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        chain.append(cursor)
        cursor = cursor.__cause__ or cursor.__context__
    text = " ".join(str(item).lower() for item in chain)
    category = ("TLS_CERTIFICATE_VERIFY_FAILED" if "certificate verify failed" in text else
        "TLS_HANDSHAKE_FAILURE" if any(isinstance(item, ssl.SSLError) for item in chain) or "ssl" in text or "tls" in text else
        "TIMEOUT" if any(isinstance(item, httpx.TimeoutException) for item in chain) else
        "PROXY_ERROR" if isinstance(error, httpx.ProxyError) else "CONNECTION_ERROR" if isinstance(error, httpx.ConnectError) else type(error).__name__)
    reasons = [getattr(item, "reason", None) for item in chain if isinstance(item, ssl.SSLError)]
    return {"failure_category": category, "exception_types": list(dict.fromkeys(type(item).__name__ for item in chain)),
        "tls_reasons": [value for value in reasons if isinstance(value, str) and re.fullmatch(r"[A-Z_0-9]+", value)]}


def child(target):
    parsed = urlsplit(target)
    if parsed.scheme != "https" or parsed.hostname not in {"dashscope.aliyuncs.com", "restapi.amap.com"} or parsed.username or parsed.password or parsed.query:
        raise ValueError("Only the two configured official unauthenticated targets are allowed")
    started = time.perf_counter()
    result = {"host": parsed.hostname, "proxy_presence": proxy_presence(), "timeout_seconds": 5, "authentication_sent": False}
    try:
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=True) as client:
            response = client.get(target, headers={"User-Agent": "BreezeTravel-unauthenticated-transport-probe/1"})
            result.update(http_status=response.status_code, failure_category=None)
    except Exception as error:
        result.update(http_status=None, **failure(error))
    result["latency_ms"] = round((time.perf_counter() - started) * 1000)
    print(json.dumps(result))


async def main(output):
    import experience
    private = experience.read_env(experience.ENV_FILE)
    launch = experience.environment(private)
    qwen = urlsplit(launch.get("QWEN_API_URL") or launch.get("QWEN_BASE_URL") or "")
    if qwen.scheme != "https" or qwen.hostname != "dashscope.aliyuncs.com":
        raise ValueError("Configured Qwen host differs from this bounded probe's official allowlist")
    targets = [urlunsplit((qwen.scheme, qwen.hostname, qwen.path or "/", "", "")), "https://restapi.amap.com/"]
    path = Path(output)
    if path.exists():
        raise FileExistsError("Preserve previous probe reports")
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": "unauthenticated-official-transport-v1", "started_at": datetime.now(timezone.utc).isoformat(),
        "proxy_environment_presence": {key: bool(launch.get(key)) for key in PROXY_KEYS},
        "private_file_proxy_declarations": {key: key in private for key in PROXY_KEYS},
        "inherited_proxy_environment_presence": {key: bool(os.environ.get(key)) for key in PROXY_KEYS},
        "ca_environment_presence": {key: bool(launch.get(key)) for key in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")},
        "constraints": {"hosts": [urlsplit(target).hostname for target in targets], "repetitions": 3, "concurrency": 2,
            "timeout_seconds": 5, "authenticated_requests": 0, "model_inference_requests": 0, "running_environment_modified": False}, "probes": []}
    lock = asyncio.Semaphore(2)
    async def probe(target, mode, repetition):
        async with lock:
            env = launch.copy()
            if mode == "HOST_ONLY_NO_PROXY":
                prior = env.get("no_proxy") or env.get("NO_PROXY") or ""
                value = ",".join(filter(None, [prior, urlsplit(target).hostname]))
                env["NO_PROXY"] = env["no_proxy"] = value
            process = await asyncio.to_thread(subprocess.run, [sys.executable, str(Path(__file__).resolve()), "--child", target],
                env=env, text=True, capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                row = json.loads(process.stdout)
            except (ValueError, TypeError):
                row = {"host": urlsplit(target).hostname, "failure_category": "PROBE_PROCESS_FAILURE", "http_status": None}
            row.update(mode=mode, repetition=repetition)
            report["probes"].append(row)
    await asyncio.gather(*(probe(target, mode, repetition) for repetition in range(1, 4)
        for target in targets for mode in ("DEFAULT_ENVIRONMENT", "HOST_ONLY_NO_PROXY")))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(path), "probe_count": len(report["probes"]), "proxy_environment_presence": report["proxy_environment_presence"],
        "results": report["probes"]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        if len(sys.argv) == 3 and sys.argv[1] == "--child":
            child(sys.argv[2])
        elif len(sys.argv) == 2:
            asyncio.run(main(sys.argv[1]))
        else:
            raise ValueError("Supply one new private output file")
    except Exception as error:
        print(json.dumps({"status": "PROBE_FAILED", "failure_category": type(error).__name__}))
        raise SystemExit(1) from None
