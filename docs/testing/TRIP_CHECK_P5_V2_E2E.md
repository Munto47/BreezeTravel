# Trip Check P5 v2 end-to-end verification

This suite keeps dataset, runner, scorer, Judge, Gate, and formal evidence claims separate. A
controlled-fixture pass is not a formal or human-evidence pass.

## Covered readbacks

- Dataset: exact JSONL bytes, canonical hashes, 360 cases, 270/90 lanes, city and split balance,
  and disjoint lineage.
- Screenshot materialization: all 171 render/OCR/cleanup receipts, receipt-to-case bindings, and
  original-image deletion. The current dataset uses `p5-development-ocr`; this is asserted as
  controlled evidence and is never promoted to actual OCR.
- Runner: all 18 pilot cases through Legacy A, Core B, and Solver C, with 54 fresh replay matches,
  exact terminal keys, exception terminalization, RunSpec whitelist, zero API/token/cost claims,
  and a 60-second local ceiling.
- Cardinality: formal nonblind is exactly 810 terminal rows, frozen blind is exactly 270, and the
  combined expectation is 1080.
- Scorer: case-level scoring reads all 54 pilot terminals. The partial run-group readback mismatch
  is a strict expected failure, not a pass.
- Isolation: frozen blind inputs contain no oracle fields; repository output paths are rejected.
- Formal/Judge/Gate: unavailable v2 evidence remains an explicit xfail or skip.

## Commands

Run from `backend` with the shared Python 3.12 environment:

```powershell
$py = 'D:\munto\code\claudeProject\agentTravel\.local-artifacts\venvs\p5-v2\Scripts\python.exe'
& $py -m pytest tests/test_trip_check_p5_v2_e2e_contract.py tests/test_trip_check_p5_v2_e2e_readback.py -q -rxXs
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

- The committed dataset says `actual_ocr=NOT_RUN`, `frozen=false`, and
  `formal_validation_eligible=false`.
- The active contract is `PENDING_V2_SEAL`, and the v2 frozen-blind seal is absent.
- Runner full-lane materialization-set hashing omits `materialization_id`, unlike the dataset and
  scorer contracts; exact 270/90 execution therefore cannot start.
- Screenshot materializations append a cleanup receipt to the provider receipt list; adapter
  readback currently validates that cleanup receipt as a `ProviderCallReceipt`, so screenshot
  terminals become `ERROR` before variant execution.
- A partial runner manifest binds the full lane files while recording the selected subset hash;
  scorer run-group validation therefore rejects the 18-case development run.
- P5 v2 Judge and Gate interfaces are not integrated. Their v1 outputs are not accepted as v2
  proof.
