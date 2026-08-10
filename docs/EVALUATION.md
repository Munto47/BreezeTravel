# Evaluation and evidence

## Fixed controlled-local suites

`python -m scripts.run_local_eval_suite` rebuilds raw case outputs under
`backend/evidence/local_eval/`. The current fixed dataset contains:

| Suite | Samples | Current result |
|---|---:|---:|
| Router | 96 | 100% |
| Task Parser | 72 | 100% |
| Verifier | 120 | 100% |
| Controlled end-to-end | 60 | 100% |
| Total | 348 | 100% |

Each suite is split into pilot/dev/blind. Case IDs and dataset/config hashes are
stored with raw outputs; summary rates include confidence intervals. Blind cases
are checked for leakage against tuning case IDs.

The end-to-end adapter passes only when no `VIOLATED` check remains. `UNKNOWN`
is retained in the output and is acceptable only when evidence is genuinely
unavailable; it is never converted to `SATISFIED`.

## Additional evidence

- `backend/evidence/fault_injection/summary.json`: 24/24 controlled failures
  produced the expected bounded retry, circuit, timeout, invalid-payload,
  persistence or degradation behavior.
- `backend/evidence/experiments/summary.json`: local RAG proxy ablations, Router
  tool-policy comparison and Verifier/Repair comparison. Raw cases, hashes and
  Pareto fields are retained. Three variants requiring an external LLM/GPU were
  deliberately recorded as not executed.
- `backend/evidence/multi_instance/summary.json`: two Python 3.11 backend
  processes, shared PostgreSQL checkpoints and atomic Redis limiting.
- Frontend controlled E2E renders all three verification states, then changes
  the itinerary and proves the stale report disappears.
- Yjs tests perform a real child-process stop/restart and read persisted Y.Doc
  content after restart.

These results are `local_real_verified` or `unit_verified`, depending on the
artifact. They are not public-deployment, external-provider or real-user proof.
Public RAG and real-user reports keep separate evidence namespaces.
