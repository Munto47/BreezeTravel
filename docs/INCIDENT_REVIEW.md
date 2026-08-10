# Controlled-local incident review

## End-to-end score regression: 60%

The first 60-case controlled E2E run passed only 60%. Two causes were found in raw
cases rather than hidden by aggregate scoring:

1. Generic policies such as “保留多数投票地点” and “RAG失败时保留实时地点并明确降级”
   were parsed as literal must-visit POIs.
2. Capacity repair could delete meal anchors, creating a new hard violation while
   resolving another one.

The parser now emits collaboration/degradation policies, the Verifier returns
`UNKNOWN` without a complete vote snapshot, and repair preserves/rebalances meal
anchors before removing ordinary slots. The rebuilt 60-case E2E suite passes
60/60 with raw reports retained.

## Random-order test contamination

A complete randomized run exposed a one-place Chengdu mock inside a Shanghai Demo
test. `test_api` patched the graph provider while importing the endpoint; the
endpoint permanently captured that mock, so behavior depended on module order.
Both suites now import consumers first and patch the symbols used by the endpoint.
Seeds 17, 42 and 91 each pass 407 tests with the same expected exclusions.

## Windows migration failure

The real migration integration test exposed that psycopg async rejects Windows'
default Proactor event loop. The migration CLI now uses `SelectorEventLoop` on
Windows and the integration test invokes the real CLI twice against a fresh
temporary database. Fresh and existing-database paths both pass and the temporary
database is dropped afterward.

## External infrastructure boundary

Pulling the pinned PostgreSQL/Redis/nginx images hit a Docker mirror TLS timeout.
PostgreSQL and Redis integration was repeated with already-cached compatible
images; nginx was not substituted with an unverified claim. This limitation is
embedded in multi-instance evidence and reliability documentation.
