# Frozen-blind seal only

This directory must not contain scoreable frozen-blind truth.

`frozen_blind.labels.jsonl` is retained as a migration-compatible filename,
but its only row is `dual-entry-sealed-label-manifest-v1` metadata. It contains
hash commitments and truth-boundary flags, not case IDs, expected findings,
metric oracles, Judge rubrics, or human labels.

The scoreable `dual-entry-blind-label-bundle-v1` belongs in CI secret artifact
storage or separately encrypted object storage outside the repository. It is
accepted only by `python -m evals.final_blind_scorer` after product outputs are
frozen and only when the external byte hash and all run/dataset/output
bindings match. Copying a bundle anywhere under the repository makes scoring
fail before the file is read.
