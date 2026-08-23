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
  controls to blind oracle fields. The scan covers known constants/functions, reversible per-case
  maps, fault-to-oracle dictionaries, answer-producing callables, and candidate-output-dependent
  label paths. This is a hard contract failure, not an xfail.
- External custody: this repository provides schemas plus a consumer-only validator. It does not
  provide a custodian/reviewer label generator. Two independent `gpt-5.6-sol` agents must create
  the bundle and review receipt with repository-external tooling and storage from the frozen
  candidate commit; neither agent may read candidate outputs.

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

After the external custodian and reviewer have independently produced their artifacts, an isolated
consumer can verify structure and commitments without deriving or judging oracle semantics:

```powershell
& $py -m scripts.validate_trip_check_p5_external_custody_v2 `
  --repo-root <candidate-readonly-worktree> `
  --external-bundle <external-bundle-path> `
  --external-bundle-sha256 <bundle-byte-sha256> `
  --external-review-receipt <external-review-receipt-path> `
  --review-receipt-sha256 <review-receipt-byte-sha256> `
  --labels-canonical-sha256 <labels-canonical-sha256> `
  --candidate-subject-commit <candidate-freeze-commit>
```

The formal seal command still receives only irreversible hashes and the candidate commit. It does
not mount or read the external label payload. The isolated final scorer remains the only tracked
consumer that validates each bundle oracle against `P5OracleV2` for scoring.

## Current non-green boundaries

- The committed candidate dataset says `actual_ocr=PASS`, `frozen=true`, and
  `formal_validation_eligible=true`; its detached-worktree formal validation passes.
- The active contract is `PENDING_V2_SEAL`, and the v2 frozen-blind seal is absent.
- The formal 810 nonblind terminals, 270 blind terminals, 1080 replays, three Judge rounds, score
  reports, and Gate manifest have not been produced. Their count and binding contracts are tested,
  but their evidence status remains `NOT_RUN`.
- The tracked fault-to-oracle mappings, oracle derivation functions, label bundle builder, reviewer
  implementation, and their answer-generating tests/CLIs have been removed. Custody and semantic
  review are now repository-external operations. The repository retains only strict schemas,
  irreversible commitments, external-path validation, and fail-closed consumers.
- No formal bundle or review receipt is generated by this change. Custody/review/seal remain
  `NOT_RUN` until the independent external agents execute against the candidate freeze.
