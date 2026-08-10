-- Public RAG corpus provenance.  Nullable fields keep existing synthetic/demo
-- notes readable while public notes can be cited end-to-end.
ALTER TABLE travel_notes
    ADD COLUMN IF NOT EXISTS source_url TEXT,
    ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS source_retrieved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS source_license TEXT,
    ADD COLUMN IF NOT EXISTS corpus_kind TEXT NOT NULL DEFAULT 'synthetic';

CREATE INDEX IF NOT EXISTS idx_travel_notes_corpus_kind
    ON travel_notes(corpus_kind);
