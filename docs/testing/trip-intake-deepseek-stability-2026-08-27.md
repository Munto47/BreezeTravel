# Trip Intake DeepSeek stability evidence — 2026-08-27

## Boundary

- Model remains `deepseek-v4-flash`; Qwen was not added.
- Default runtime remains deterministic. Hybrid mode is explicit for evaluation and local E2E only.
- Scope is Trip Intake plus the minimum downstream fixes required to execute the existing Trip Check chain.
- Original 24 frozen blind inputs and oracle were not modified. The one-shot product prediction is bound to clean commit `d4fd9aafcb9dc12156e2ba4f0199c822f15f7c41`.
- The user separately approved migration 027 after the initial diagnostic E2E identified the conflicting constraint. No public API, production dependency, public deployment, H1, release, or `main` merge is included.

## Candidate quality evidence

| Lane | Cases | Gate | Locations | Party | Duration | Preferences | Contract | Critical errors | P95 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Regression | 7 | PASS | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0 | 4.210 s |
| Dev | 72 | PASS | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0 | 4.109 s |
| Validation v2 | 24 | PASS | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0 | 4.513 s |
| Frozen blind | 24 | REJECT | 0.4667 | 0.8583 | 0.8711 | 0.5808 | 0.7583 | 23 | 3.691 s |

Validation v2 contains 24 cases in eight isolated three-case families. Maximum text similarity to the original 120 cases is 0.801418. Its single scored run for the accepted candidate had three safe fallbacks (one schema-invalid and two timeouts); fallback behavior remained part of the receipt rather than being hidden.

The shared `OBSERVE_ONLY` ledger contained 202 calls after Validation, 324333 input tokens, 112720 output tokens, and an estimated cumulative cost of 1.87374432 CNY. It had nullable limits and retained reservations, errors, actual model readback, token use, latency, and estimated cost.

## Engineering evidence

- Backend: `2015 passed, 32 skipped` after migration 027.
- PostgreSQL migration integration: `2 passed` for fresh and existing database paths.
- Ruff: PASS for `app`, `tests`, `scripts`, and `evals`.
- Frontend production build: PASS.
- Dual-entry validator: structurally valid; existing release blockers remain unchanged and do not become Trip Intake proof.
- Original 120-case validator: PASS, evidence span validity 1.0, `blind_labels_read=false`.
- Remediation validator: PASS, regression 7, Validation 24, family count 8, frozen blind input hash unchanged.

## Local real-model E2E boundary

The isolated Docker stack exercised Beijing, Shanghai, and Hangzhou through login, room creation, text Intake, real DeepSeek extraction, confirmation, materialization, place resolution, Audit, Advice, repair adoption, new revision, and full postcheck. The diagnostic run obtained 3/3 chains, actual model `deepseek-v4-flash`, normal-chain fallback 0, no unexpected 5xx, no duplicate revision or side effect, idempotent replay, refresh recovery, SSE reconnect, schema-invalid and timeout fallback, and Provider partial-failure `UNKNOWN` retention.

After explicit user approval, migration `027_trip_intake_revision_lineage.sql` removed only the conflicting generated constraint. A new Compose project with a new PostgreSQL volume applied 001 through 027 from scratch; the migration ledger contained 027 and the conflicting constraint count was zero. The formal E2E bound to `ee686a517e37019c06a3fa4c9ddb87b2355567ea` then passed Beijing, Shanghai, and Hangzhou 3/3 with real `deepseek-v4-flash`, normal-chain fallback 0, revision 1 to 2, successful postcheck, idempotent replays, refresh recovery, SSE reconnect, fault fallbacks, Provider partial-failure UNKNOWN preservation, and zero unexpected 5xx.

## Remaining gate

The external label artifact was found outside the repository and matched the sealed SHA-256 exactly. The isolated scorer evaluated only the already frozen predictions and returned `REJECT`: hallucination 19, negation reversal 4, locations 0.4667, party 0.8583, duration 0.8711, preferences 0.5808, contract controls 0.7583. No case details were emitted. Product inference will not be rerun and no tuning will use blind truth.

The current Goal therefore terminates as `REJECTED` even though the engineering and local E2E gates pass. A future attempt requires a separately approved Goal and a newly governed blind version; this run cannot be promoted by changing thresholds, inspecting per-case truth, or retrying the model.

`INTAKE_V2_DEVELOPMENT_READY=false` and `V1_CANDIDATE_READY=false` until the applicable remaining gates are actually run and pass.
