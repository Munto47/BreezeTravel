# Continuous import HTTP slice

`run-import-http` executes the `pr_offline` RunSpec through the public product
HTTP API only. It creates an authenticated room and workspace, imports text,
performs label-authorized ambiguity confirmation when applicable, attempts the
apply operation, executes the case-declared Audit and bounded Repair/postcheck
HTTP chain, and reads the import/workspace state back.

```powershell
cd backend
python -m evals.continuous run-import-http `
  --spec evals/run_specs/dual-entry-pr-offline.json
```

The runner never imports product-domain modules, never seeds SQL, never reads
the frozen-blind label path, and records every HTTP request status and latency.
Authorization tokens are redacted. It writes `product_outputs.jsonl`,
`provider_receipts.jsonl`, `http_transactions.jsonl`, deterministic scores,
bad cases, zero-cost accounting, and the final gate to a unique evidence run
directory.

Both HTTP runners project public readbacks into the six structured
`metric_oracles` units and call the same pure deterministic scorer. N/A oracles
never enter a metric denominator. A required product field that is absent is
`UNSCORED` at metric level and makes the case/run `INVALID`; an empty predicted
set is a real zero only when the corresponding field was present. Aggregates
persist raw numerator/denominator, applicable/scored/invalid/N/A case IDs, and
coverage. A configured metric threshold fails closed when there is no
applicable denominator or coverage is incomplete. Development runners continue
to reject `frozen_blind` and `sealed` labels; blind labels are reserved for a
separate isolated scorer process after product outputs are frozen.

## Frozen-blind isolated scorer

`frozen_blind` has no scoreable repository label file. The checked-in
`labels_seal` is metadata only. A release runner freezes `run_spec.json` and
`product_outputs.jsonl` without mounting truth. Scoring is a separate command:

```powershell
cd backend
$env:BREEZE_BLIND_BUNDLE_PATH = "D:\isolated-secrets\frozen-blind.bundle.json"
$env:BREEZE_BLIND_BUNDLE_SHA256 = "<sha256 supplied by the secret store>"
python -m evals.final_blind_scorer `
  --repo-root .. `
  --run-dir evidence/runs/<run_id> `
  --output evidence/runs/<run_id>/blind_score.json
```

An isolated orchestrator may instead pipe the same bundle to `--bundle -`.
There is deliberately no default repository path and no `evals.continuous`
subcommand that loads blind truth. The scorer rejects a bundle path under the
repository before reading it. It then verifies the independently supplied
bundle byte hash, the checked-in canonical-label commitment, exact selected
case set, dataset/manifest bytes, frozen product-output bytes, `run_id`, and
the exact `run_spec.json` byte hash. A missing artifact, stale binding,
duplicate/missing case, malformed actual, or incomplete metric coverage is
`INVALID/REJECT`; no force or partial-score path exists.

The bundle must declare `evidence_class=controlled_blind_oracle` and
`human_evidence=false`. It is deterministic synthetic/controlled truth, never
human calibration. Generator, SUT, and semantic Judge inputs must be created
before the bundle is mounted and must never contain the bundle or its labels.

Execution is fail closed. An invalid preflight, unavailable localhost, disabled
test login, malformed response, missing provider receipt, or failed case keeps
the gate `INVALID/REJECT`; a completely passing HTTP slice is `PASS/PROMOTE`.
Wrong-city candidates are collected only from the product's explicit
`rejected_candidates[].resolved_place_receipt` readback and retain the
`REJECTED/WRONG_CITY` disposition. Empty results and incomplete candidates are
never converted into receipts. The current checked-in RunSpec also remains blocked
at preflight until referenced source documents have concrete raw and extract
hashes; the runner does not bypass that evidence boundary.

## Builder SuggestionSet HTTP slice

`run-builder-http` drives the Builder through public HTTP boundaries only:
test login, room creation, workspace creation with an explicit controlled
revision-one seed, SuggestionSet creation, exact frozen-set GET readback,
candidate accept with `If-Match` and `Idempotency-Key` and no client Place
body, candidate preview, candidate dismiss with a reason, line completion,
revision/new-anchor continuation, recommendation-event readback, Undo when
requested, final snapshot, and fresh-client resume readback.

```powershell
cd backend
python -m evals.continuous run-builder-http `
  --spec evals/run_specs/dual-entry-builder-http-slice.json
```

The checked-in RunSpec selects the three G2 four-stop seeds and all six G5
recovery seeds from the 78-case corpus. G0 binds it to the checked-in Beijing,
Shanghai, and Hangzhou local-authorized capture by repository-relative
`snapshot_path`, exact file-byte SHA-256, and the artifact's canonical payload
SHA-256 as `snapshot_id`. Preflight rejects path escape, missing files, byte-hash
mismatch, wrong evidence class/subtype/status, failed integrity, payload
tampering, or an unbound snapshot ID. The cache namespace is resolved only
after these bindings are verified.

This G0 binding proves only that a local-authorized Amap candidate-and-walking-
route capture is present and intact. It is not opening-hours proof, public-
internet E2E evidence, human evidence, or release approval. No embedded
`candidate_snapshot` is posted to the product; candidates, ranks, canonical
facts, and receipts must all come back from the product's configured ranked
provider.

Each run writes `product_outputs.jsonl`, `provider_receipts.jsonl`,
`recommendation_events.jsonl`, `http_transactions.jsonl`, deterministic
scores, bad cases, zero-cost accounting, and `gate.json`. Normal sets require
4–6 visible candidates and reject wrong-city, failed HARD-gate, stale, or
UNKNOWN route/evidence candidates leaking into Top 3. Create/GET frozen hashes,
one-revision atomic accept, new anchor, receipt completeness, event correlation,
and idempotent replay are scored. Preview, dismiss, and line-completed commands
are sent to their public product endpoints with `Idempotency-Key`. Their response
must carry the server-frozen session/workspace/context/policy/provider snapshot,
candidate/rank where applicable, current revision, server actor, event id, and
timestamp. The same command is replayed with the same key and must return the
exact same event receipt; the subsequent event-ledger GET must contain it.
Clients do not submit any of those authority fields. Dismiss submits only its
bounded `reason_code`.

Controlled backend/Yjs restart still has no public operation. It is recorded as
`UNSUPPORTED` and forces `INVALID/REJECT`; a resume GET is not reported as proof
of a process restart. Drag/button/concurrency scenarios are likewise outside
this SuggestionSet/event/accept slice. Provider 503, interaction command error,
frozen-context mismatch, broken idempotent replay, missing ledger event,
stale/expired/tampered accept, 409 rollback, missing receipts, unavailable
localhost, or any unsupported required capability fail closed. Authorization
tokens are redacted from recorded transactions. The runner does not import
product-domain code or execute SQL seeds.

The checked-in artifact is not itself a configured product snapshot adapter.
Therefore G0 can pass while execution still fails closed when localhost,
PostgreSQL, or the product's frozen-snapshot adapter is unavailable. Such a run
must not be reported as G2 executed.
