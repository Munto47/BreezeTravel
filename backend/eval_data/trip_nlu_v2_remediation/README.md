# Trip NLU v2 remediation data

This directory is separate from the original 120-case dataset. `regression.jsonl`
contains renamed non-blind failures from the two rejected Validation runs.
`validation_v2.jsonl` is a newly salted, family-isolated 24-case candidate gate.

The original `trip_nlu_v2/frozen_blind.inputs.jsonl` and its external labels are
not generated, modified, or read by this package. Generate and validate this pack
with:

```powershell
cd backend
python -m scripts.generate_trip_nlu_v2_remediation
python -m scripts.validate_trip_nlu_v2_remediation
```
