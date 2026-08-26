# Trip NLU v2 dataset

This directory is an executable specification for text requirement extraction only. It does not prove OCR accuracy, Provider facts, itinerary reasonableness, nationwide coverage, or human validation.

The repository contains labelled `dev.jsonl` and `validation.jsonl`, plus truth-free `frozen_blind.inputs.jsonl`. Frozen-blind labels must remain outside the repository. The metadata-only seal commits to their SHA-256.

Generation is truth-first. The first 60 cases use the deterministic renderer. The other 60 use a separate renderer compiled from the user-supplied generation prompt; regeneration must read the exact prompt bytes whose SHA-256 is recorded in `generator_registry.json`.

```powershell
cd backend
python -m scripts.generate_trip_nlu_v2 `
  --external-blind-labels D:\secure\trip-nlu-v2-blind-labels\frozen_blind.labels.jsonl `
  --user-prompt C:\secure\pasted-text.txt

python -m evals.trip_nlu_v2.validator eval_data/trip_nlu_v2 `
  --external-blind-labels D:\secure\trip-nlu-v2-blind-labels\frozen_blind.labels.jsonl `
  --finalize-isolated-receipt

python -m scripts.validate_trip_nlu_v2
```

Formal blind scoring uses `python -m evals.trip_nlu_v2.gate`. Its RunSpec must contain a non-empty `run_id`; model name/version and prompt/schema/config hashes; and exact dataset-manifest, blind-label, product-output, validator and scorer hashes. The aggregate receipt contains no per-case truth.
