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

## Three-city local RC1 candidate

The RC1 recommendation claim is deliberately limited to Beijing, Shanghai and
Hangzhou, with 50 fixed cases per city. Iteration uses a frozen live-candidate
snapshot and must record zero provider, generation-LLM and Judge-API calls.
After three identical 150-case replays, three independent GPT-5.6-sol Codex
subagents judge the same blind bundle. Their reports are hash-bound to the
dataset, replay report, rubric and backend execution tree.

This is a model-panel consistency check, not human calibration. The three
rounds passed 146/150, 145/150 and 142/150; majority voting passed 146/150 and
full agreement was 95.33%. The agreement gate passed, but the overall quality
gate did not: round three passed only 5/9 `all` cases. The allowed wording is
“the independent model panel met the agreement threshold; the quality gate
did not pass and human calibration was not performed.” DeepSeek remains part
of the real product generation chain, but it is never used as the Judge.

The current frozen candidate is `rc1_v22`. Its three full replays each pass
150/150 with the same normalized output hash and with provider, generation-LLM
and Judge-API calls all equal to zero. Two Hangzhou cases fail closed because
the frozen provider pool contains no valid nearby food: this is recorded as a
1.33% missing-category rate, below the predefined 2% ceiling. The cards do not
substitute a scenic POI or a meal more than five kilometres away, so the
wrong-category rate is zero. A safe degradation counts as an automatic pass
only when the response carries the explicit fail-closed receipt; the missing
category remains separately visible in `summary.category_coverage`.

The source tree also contains post-v22 hardening for pairwise-compact
attraction/food/hotel cores, spatially redundant attraction removal and
short-distance vehicle-transfer wording. These changes were added after the
bound v22 reports and were not followed by another 150-case replay or model
panel. They are therefore implementation hardening, not updated evaluation
evidence.
