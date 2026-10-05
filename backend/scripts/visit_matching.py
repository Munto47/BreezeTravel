"""One-to-one occurrence matching, independent of generated identifiers."""


def same_occurrence(gold, actual):
    if actual.get("name") not in set(gold.get("acceptable_names", [])) | {gold["name"]}:
        return False
    if "span_start" not in gold:
        return actual.get("day_index") == gold.get("day_index")
    spans = [(gold["span_start"], gold["span_end"])] + [
        (span["span_start"], span["span_end"]) for span in gold.get("equivalent_reference_spans", [])]
    return isinstance(actual.get("span_start"), int) and isinstance(actual.get("span_end"), int) and any(
        actual["span_start"] <= start < end <= actual["span_end"] for start, end in spans)


def match_occurrences(expected, actual, relation_matches):
    from collections import deque
    n, m = len(expected), len(actual)
    source, sink = n + m, n + m + 1
    graph = [[] for _ in range(n + m + 2)]
    def edge(u, v, cost):
        forward = [v, len(graph[v]), 1, cost]
        reverse = [u, len(graph[u]), 0, -cost]
        graph[u].append(forward)
        graph[v].append(reverse)
    for i, gold in enumerate(expected):
        edge(source, i, 0)
        for j, item in enumerate(actual):
            if same_occurrence(gold, item):
                cost = (0 if relation_matches(gold, item) else 100000) + abs(i - item.get("sequence_index", j))
                edge(i, n + j, cost)
    for j in range(m):
        edge(n + j, sink, 0)
    while True:
        distance, previous = [float("inf")] * len(graph), [None] * len(graph)
        distance[source] = 0
        pending, queued = deque([source]), {source}
        while pending:
            u = pending.popleft()
            queued.remove(u)
            for k, (v, _, capacity, cost) in enumerate(graph[u]):
                if capacity and distance[u] + cost < distance[v]:
                    distance[v], previous[v] = distance[u] + cost, (u, k)
                    if v not in queued:
                        pending.append(v)
                        queued.add(v)
        if previous[sink] is None:
            break
        v = sink
        while v != source:
            u, k = previous[v]
            item = graph[u][k]
            item[2] -= 1
            graph[v][item[1]][2] += 1
            v = u
    return [(i, item[0] - n) for i in range(n) for item in graph[i]
            if n <= item[0] < n + m and item[2] == 0]


def validate_formal_annotation(label):
    if not label.get("acceptance_eligible") or label.get("unresolved_disputes"):
        raise ValueError("formal annotations must be eligible and resolved")
    required = {"id", "name", "span_start", "span_end", "day_index", "role", "parent_id",
                "replaces_id", "choice_group_id", "branch_id", "purpose", "order_index", "identity_requirement"}
    activities = label["activities"]
    ids = {item.get("id") for item in activities}
    if len(ids) != len(activities):
        raise ValueError("visit identifiers must be unique")
    for item in activities:
        if not required <= item.keys():
            raise ValueError("formal visit annotations are incomplete")
        if any(item[field] is not None and item[field] not in ids for field in ("parent_id", "replaces_id")):
            raise ValueError("formal visit relation has no target")
        if item["identity_requirement"] not in {"verified", "pending", "not_applicable"}:
            raise ValueError("identity requirement must be explicit")
        if item["identity_requirement"] == "verified" and not item.get("expected_poi_ids"):
            raise ValueError("verified identity needs reviewed acceptable identities")


def validate_acceptance_corpus(cases, labels, excluded_families):
    from collections import Counter
    quotas = {"北京": 8, "上海": 7, "深圳": 8, "广州": 7}
    if Counter(case["city"] for case in cases) != quotas:
        raise ValueError("formal corpus must contain the complete four-city allocation")
    if len({case["id"] for case in cases}) != 30:
        raise ValueError("formal corpus requires thirty distinct articles")
    for city in quotas:
        if len({case["family_id"] for case in cases if case["city"] == city}) < 3:
            raise ValueError("each city requires at least three independent source families")
    for case in cases:
        if case["family_id"] in excluded_families or not case.get("source_url"):
            raise ValueError("formal sources must be public and independent of development and demos")
        label = labels.get(case["id"], {})
        if label.get("source_text") != case["text"]:
            raise ValueError("every formal source requires unchanged reviewed annotations")
        validate_formal_annotation(label)


def whole_article_passed(row):
    return (row.get("status") == "COMPLETED" and row.get("semantic_exact") is True
            and row.get("visibility_assessed") is True and (row.get("coverage") or {}).get("complete") is True
            and row.get("unprocessed_count") == 0 and row.get("unassessed_auto_confirmations") == 0
            and row.get("wrong_auto_confirmations") == 0)
