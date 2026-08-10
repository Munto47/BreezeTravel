-- Reproducible public-corpus provenance.  Synthetic notes remain readable,
-- while every public record can be traced back to an immutable upstream revision.
ALTER TABLE travel_notes
    ADD COLUMN IF NOT EXISTS source_revision TEXT,
    ADD COLUMN IF NOT EXISTS source_content_hash TEXT,
    ADD COLUMN IF NOT EXISTS source_attribution TEXT;

CREATE INDEX IF NOT EXISTS idx_travel_notes_public_source
    ON travel_notes(corpus_kind, source_revision);
