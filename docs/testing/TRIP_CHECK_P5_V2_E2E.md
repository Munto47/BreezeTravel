# Trip Check P5 v2 end-to-end verification

This suite keeps dataset, runner, scorer, Judge, Gate, and formal evidence claims separate. A
controlled-fixture pass is not a formal or human-evidence pass.

## Covered readbacks

- Dataset: exact JSONL bytes, canonical hashes, 360 cases, 270/90 lanes, city and split balance,
  disjoint lineage, and a formal validator run from a fresh detached `HEAD` worktree.
- Screenshot materialization: all 171 render/OCR/cleanup receipts, receipt-to-case bindings, and
  original-image deletion. The candidate freeze binds PaddleOCR 3.7.0 actual materialization and
  records `actual_ocr=PASS`; no original screenshot bytes are tracked.
- Runner: all 18 pilot cases through Legacy A, Core B, and Solver C, with 54 fresh replay matches,
  exact terminal keys, exception terminalization, RunSpec whitelist, zero API/token/cost claims,
  a 60-second local ceiling, and readback of subject, dataset, case/materialization, terminal,
  per-variant, RunSpec, and replay commitments.
- Cardinality: formal nonblind is exactly 810 terminal rows, frozen blind is exactly 270, and the
  combined expectation is 1080. This is an executable cardinality contract, not a claim that the
  formal outputs already exist.
- Scorer: case-level scoring reads all 54 pilot terminals. The partial run-group readback mismatch
  is a strict expected failure, not a pass.
- Isolation: frozen blind inputs contain no oracle fields; repository output paths are rejected.
- Formal/Judge/Gate: the real `judge_v2` and `gate_v2` interfaces and CLIs are integrated. While
  the seal is absent, each formal entry point is required to fail with
  `P5_V2_FORMAL_CONTRACT_NOT_READY`; no formal-readiness xfail or skip remains.
- Supersession: v1 runner, scorer, and Gate CLIs reject before reading artifact paths with
  `P5_V1_FORMAL_CONTRACT_SUPERSEDED`.
- Blind leak scan: tracked candidate code must not contain a derivation path from public fault
  controls to blind oracle fields. This is a hard contract failure, not an xfail.

## Commands

Run from `backend` with the shared Python 3.12 environment:

```powershell
$py = 'D:\munto\code\claudeProject\agentTravel\.local-artifacts\venvs\p5-v2\Scripts\python.exe'
& $py -m pytest tests/test_trip_check_p5_v2_e2e_contract.py tests/test_trip_check_p5_v2_e2e_readback.py -q -ra
& $py -m ruff check tests/p5_v2_e2e_helpers.py tests/test_trip_check_p5_v2_e2e_contract.py tests/test_trip_check_p5_v2_e2e_readback.py
```

The actual PaddleOCR boundary is opt-in because it loads the external/GPU-class OCR dependency:

```powershell
$env:RUN_EXTERNAL_TESTS = '1'
$env:P5_V2_ACTUAL_OCR_SAMPLE = '1'
& $py -m pytest tests/test_trip_check_p5_v2_e2e_contract.py -k actual_ocr -q -rxXs
```

The runner CLI must be invoked as a module from `backend` so the `evals` package is importable:

```powershell
& $py -m scripts.run_trip_check_p5_v2_eval --lane nonblind --replay --output-dir <external-path>
```

## Current non-green boundaries

- The committed candidate dataset says `actual_ocr=PASS`, `frozen=true`, and
  `formal_validation_eligible=true`; its detached-worktree formal validation passes.
- The active contract is `PENDING_V2_SEAL`, and the v2 frozen-blind seal is absent.
- The formal 810 nonblind terminals, 270 blind terminals, 1080 replays, three Judge rounds, score
  reports, and Gate manifest have not been produced. Their count and binding contracts are tested,
  but their evidence status remains `NOT_RUN`.
- `blind_custody_v2.py` currently contains fault-to-oracle maps plus
  `derive_blind_oracle_v2`/`derive_all_blind_labels_v2`. Because frozen case controls and
  materializations are public, these symbols let repository readers reconstruct the blind label
  payload. This violates the Goal invariant that the repository contain neither blind labels nor
  a reversible answer path, so sealing must stop.

The minimum safe correction is to move the fault-to-oracle mappings and all oracle derivation into
repository-external custodian/reviewer tooling. The tracked repository may retain strict schemas,
external artifact hashes, review/seal verification, and fail-closed consumers, but it must not be
able to regenerate the 90 labels from tracked inputs. Custody/review/seal must be rerun only after
that hard leak test is green.
