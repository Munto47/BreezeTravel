"""Suite-wide test isolation established before application modules import."""

import os

import pytest


# Tests own their process environment.  Explicit assignment prevents a local
# .env (for example FT_ROUTER_ENABLED=true) from turning a unit run into a GPU
# model evaluation.
os.environ["RUNTIME_PROFILE"] = "test"
os.environ["DEMO_MODE"] = "true"
os.environ["AMAP_MOCK"] = "true"
os.environ["REQUIRE_SCHEMA_CHECK"] = "false"
os.environ["FT_ROUTER_ENABLED"] = "false"
os.environ["PLACE_META_LOOKUP_ENABLED"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: deterministic test without network or database")
    config.addinivalue_line("markers", "integration: test using controlled service dependencies")
    config.addinivalue_line("markers", "local_e2e: local full-stack browser or API flow")
    config.addinivalue_line("markers", "external: real provider, GPU or public network validation")


def pytest_collection_modifyitems(items):
    """Classify legacy tests so CI cannot accidentally call providers/GPU."""
    run_external = os.environ.get("RUN_EXTERNAL_TESTS") == "1"
    for item in items:
        node_id = item.nodeid.replace("\\", "/")
        if "test_migrations_integration.py" in node_id or "test_service_integration.py" in node_id:
            item.add_marker(pytest.mark.integration)
        elif "test_router_ft.py" in node_id or "evaluate_rag_pipeline" in node_id:
            item.add_marker(pytest.mark.external)
        elif not any(item.get_closest_marker(name) for name in ("unit", "integration", "local_e2e", "external")):
            item.add_marker(pytest.mark.unit)
        if item.get_closest_marker("external") and not run_external:
            item.add_marker(pytest.mark.skip(reason="set RUN_EXTERNAL_TESTS=1 for provider/GPU tests"))


@pytest.fixture
def fresh_settings():
    from app.config import clear_settings_cache, get_settings

    clear_settings_cache()
    value = get_settings()
    yield value
    clear_settings_cache()
