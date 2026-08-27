# Trip Intake DeepSeek stability evidence — 2026-08-27

## Boundary

- Model remains `deepseek-v4-flash`; Qwen was not added.
- Default runtime remains deterministic. Hybrid mode is explicit for evaluation and local E2E only.
- Scope is Trip Intake plus the minimum downstream fixes required to execute the existing Trip Check chain.
- Original 24 frozen blind inputs and oracle were not modified. The one-shot product prediction is bound to clean commit `d4fd9aafcb9dc12156e2ba4f0199c822f15f7c41`; scoring remains `EVIDENCE_INCOMPLETE` because the external labels are not provisioned.
- No public API, production dependency, repository migration, public deployment, H1, release, or `main` merge is included.

## Candidate quality evidence

| Lane | Cases | Gate | Locations | Party | Duration | Preferences | Contract | Critical errors | P95 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Regression | 7 | PASS | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0 | 4.210 s |
| Dev | 72 | PASS | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0 | 4.109 s |
| Validation v2 | 24 | PASS | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0 | 4.513 s |

Validation v2 contains 24 cases in eight isolated three-case families. Maximum text similarity to the original 120 cases is 0.801418. Its single scored run for the accepted candidate had three safe fallbacks (one schema-invalid and two timeouts); fallback behavior remained part of the receipt rather than being hidden.

The shared `OBSERVE_ONLY` ledger contained 202 calls after Validation, 324333 input tokens, 112720 output tokens, and an estimated cumulative cost of 1.87374432 CNY. It had nullable limits and retained reservations, errors, actual model readback, token use, latency, and estimated cost.

## Engineering evidence

- Backend: `2014 passed, 32 skipped`.
- Ruff: PASS for `app`, `tests`, `scripts`, and `evals`.
- Frontend production build: PASS.
- Dual-entry validator: structurally valid; existing release blockers remain unchanged and do not become Trip Intake proof.
- Original 120-case validator: PASS, evidence span validity 1.0, `blind_labels_read=false`.
- Remediation validator: PASS, regression 7, Validation 24, family count 8, frozen blind input hash unchanged.

## Local real-model E2E boundary

The isolated Docker stack exercised Beijing, Shanghai, and Hangzhou through login, room creation, text Intake, real DeepSeek extraction, confirmation, materialization, place resolution, Audit, Advice, repair adoption, new revision, and full postcheck. The diagnostic run obtained 3/3 chains, actual model `deepseek-v4-flash`, normal-chain fallback 0, no unexpected 5xx, no duplicate revision or side effect, idempotent replay, refresh recovery, SSE reconnect, schema-invalid and timeout fallback, and Provider partial-failure `UNKNOWN` retention.

This is not a formal clean-schema E2E PASS. Migration 026 currently declares `UNIQUE (room_id, intake_id)` on `trip_intake_revisions`, so a fresh database rejects revision 2 for the same intake. The diagnostic run proceeded only after dropping that constraint inside the isolated disposable database. The repository migration was not changed because this Goal explicitly excludes migration/schema changes and project policy requires user approval.

## Remaining gate

The one-shot frozen blind product run completed 24/24 with actual model readback 24/24, P95 3.691 seconds, one schema-invalid safe fallback, and prediction SHA-256 `52294de76511ec144caf94b22e2325388e942518c567256a3f2b3559c64b9d11`. Its model quality gate is not scored and is not a PASS: the repository contains only the metadata seal, while the required external label artifact was not mounted.

1. Mount the external frozen blind label file outside the repository and run only the isolated scorer against the already frozen predictions; do not rerun product inference.
2. If blind passes, authorize the narrow migration change needed to remove or supersede the conflicting uniqueness constraint.
3. Recreate a fresh Docker database from migrations and rerun the complete E2E without a local schema hotfix.

`INTAKE_V2_DEVELOPMENT_READY=false` and `V1_CANDIDATE_READY=false` until the applicable remaining gates are actually run and pass.
