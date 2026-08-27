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

## Confirmation and browser E2E follow-up

The confirmation hotfix is bound to product subject commit `660cb1804cef74d43a269eb28892a6df29995004`. It converts incomplete READY validation failures into a stable business error, persists corrected city/date/party values before confirmation, exposes initial frozen-candidate place search, waits for authoritative terminal run state, and reconciles adoption of either offered repair without duplicate revisions.

The final three-city API receipt passed Beijing, Shanghai, and Hangzhou `3/3` with actual model `deepseek-v4-flash`, normal-chain fallback 0, 82 HTTP steps, no unexpected 5xx, revision 1 to 2, successful postcheck, SSE reconnect, idempotent replay, timeout/schema-invalid fallback, and Provider partial-failure `UNKNOWN` retention. Its SHA-256 is `5b296d489a3c47533a718174f77c75d483ed45c79bf887c157eb18a7dd4d552b`.

The in-app browser independently completed a Beijing test-data chain through Intake, confirmation, materialization, frozen-candidate place binding, Audit, Advice, repair adoption, new revision, and full postcheck. Reload readback showed revision 2 and authoritative `SUCCEEDED/POSTCHECK`; browser-facing logs contained neither the Pydantic validation leak nor `crypto.randomUUID is not a function`, and the browser backend recorded zero 5xx responses.

Final verification after the follow-up fixes: backend `2019 passed, 32 skipped`; Ruff PASS; frontend production build PASS; candidate-bound original 120-case validator PASS with evidence validity 1.0 and `blind_labels_read=false`; remediation validator PASS with frozen blind hash unchanged. These results complete the local hotfix Goal only. The frozen blind result remains `REJECT`, so `INTAKE_V2_DEVELOPMENT_READY=false` and `V1_CANDIDATE_READY=false` remain unchanged.

## Confirmed date-range duration follow-up

A real in-app-browser recovery exposed an inconsistent correction revision: the user-confirmed city, party size, and complete `2026-08-27` to `2026-08-30` date range were exact, while `temporal.days` remained `UNKNOWN` with one stale blocking issue. Product subject commit `b53d5e638611f0df3d24bb0576f56ac0c5267e6a` now derives the inclusive duration from a complete confirmed date range and removes only the resolved `temporal.days` blocker. Missing core fields and unrelated conflicts remain blocking.

The existing revision 5 was recovered without another correction source: in-app-browser confirmation created revision 6 as `READY`, with exact four days and zero blocking issues, then materialized it into the authoritative import flow. Database readback matched the browser state.

Post-fix verification passed `2020` backend tests with `32` skipped, Ruff, and the frontend production build. A fresh three-city real-model run bound to the product subject commit passed Beijing, Shanghai, and Hangzhou `3/3`, used actual `deepseek-v4-flash` with normal-chain fallback 0, completed 82 HTTP steps with zero unexpected 5xx, and passed schema-invalid/timeout fallback, idempotent replay, SSE reconnect, Provider partial-failure retention, revision adoption, and full postcheck. Receipt SHA-256: `afdda84d98985aa6e1fa23be51fb9ef77abf92d3f5eb7d21a430f5b126fdd678`.
