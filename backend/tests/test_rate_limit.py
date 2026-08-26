
from app.api import rate_limit


def test_memory_limiter_is_a_real_sliding_window():
    rate_limit._windows.clear()
    assert rate_limit._memory_allowed("1.2.3.4", 2, 100.0)
    assert rate_limit._memory_allowed("1.2.3.4", 2, 101.0)
    assert not rate_limit._memory_allowed("1.2.3.4", 2, 102.0)
    assert rate_limit._memory_allowed("1.2.3.4", 2, 161.0)


def test_forwarded_header_requires_explicit_trust(monkeypatch):
    from app.config import settings

    class Client:
        host = "10.0.0.1"

    class Request:
        client = Client()
        headers = {"x-forwarded-for": "203.0.113.1, 10.0.0.1"}
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    assert rate_limit._client_ip(Request()) == "10.0.0.1"
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    assert rate_limit._client_ip(Request()) == "203.0.113.1"
