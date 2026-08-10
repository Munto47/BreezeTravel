from app import metrics


def test_labelled_metrics_do_not_require_external_services():
    before = metrics.snapshot()["labelled"]["tool_outcomes"].get("search_places:ok", 0)
    metrics.observe("tool_outcomes", "search_places:ok")
    assert metrics.snapshot()["labelled"]["tool_outcomes"]["search_places:ok"] == before + 1
