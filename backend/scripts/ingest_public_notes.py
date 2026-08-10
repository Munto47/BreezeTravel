"""Ingest a reviewed public-source corpus without scraping or hiding provenance.

Input is JSONL.  Each line must be reviewed by a human and contain the full
excerpt that may be indexed, its canonical URL, licence/use boundary, city and
retrieval timestamp.  This script deliberately does not crawl websites: that
keeps copyright, robots and source-quality decisions explicit.
"""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from openai import AsyncOpenAI

from app.config import settings
from scripts.ingest_notes import split_into_chunks, _tokenize_chinese, EMBEDDING_BATCH


REQUIRED = {"id", "title", "city", "content", "source_url", "source_license", "source_revision", "source_attribution"}
ALLOWED_LICENSES = {"CC BY-SA 4.0", "CC0 1.0"}


def load_records(path: Path) -> list[dict]:
    records: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        item = json.loads(line)
        missing = REQUIRED - item.keys()
        if missing:
            raise ValueError(f"line {line_number}: missing {sorted(missing)}")
        if len(item["content"]) < 80:
            raise ValueError(f"line {line_number}: content is too short to be a useful source")
        if item["source_license"] not in ALLOWED_LICENSES:
            raise ValueError(f"line {line_number}: unsupported public-source licence")
        if item.get("corpus_kind", "public") != "public":
            raise ValueError(f"line {line_number}: public importer only accepts corpus_kind=public")
        item.setdefault("source_retrieved_at", datetime.now(timezone.utc).isoformat())
        for key in ("source_published_at", "source_retrieved_at"):
            if item.get(key):
                item[key] = datetime.fromisoformat(str(item[key]).replace("Z", "+00:00"))
        item.setdefault("tags", ["public-source"])
        records.append(item)
    if not records:
        raise ValueError("No source records found")
    return records


async def ingest(records: list[dict]) -> None:
    if not settings.effective_embedding_api_key:
        raise RuntimeError("EMBEDDING_API_KEY (or OPENAI_API_KEY fallback) is required")
    client = AsyncOpenAI(api_key=settings.effective_embedding_api_key, base_url=settings.effective_embedding_api_url)
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        items = []
        for record in records:
            for idx, chunk in enumerate(split_into_chunks(record["content"])):
                items.append((record, idx, chunk["text"], _tokenize_chinese(chunk["text"])))
        vectors: list[list[float]] = []
        for offset in range(0, len(items), EMBEDDING_BATCH):
            batch = items[offset:offset + EMBEDDING_BATCH]
            response = await client.embeddings.create(model=settings.embedding_model, input=[item[2] for item in batch])
            vectors.extend(item.embedding for item in response.data)
        async with pool.acquire() as conn:
            for record in records:
                await conn.execute(
                    """INSERT INTO travel_notes (id,title,city,content,tags,source_url,source_published_at,source_retrieved_at,source_license,corpus_kind,source_revision,source_content_hash,source_attribution)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'public',$10,$11,$12)
                    ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, content=EXCLUDED.content,
                    source_url=EXCLUDED.source_url, source_published_at=EXCLUDED.source_published_at,
                    source_retrieved_at=EXCLUDED.source_retrieved_at, source_license=EXCLUDED.source_license,
                    source_revision=EXCLUDED.source_revision, source_content_hash=EXCLUDED.source_content_hash,
                    source_attribution=EXCLUDED.source_attribution, corpus_kind='public'""",
                    record["id"], record["title"], record["city"], record["content"], record["tags"],
                    record["source_url"], record.get("source_published_at"), record["source_retrieved_at"], record["source_license"],
                    record["source_revision"], record.get("source_content_hash"), record["source_attribution"],
                )
            for (record, idx, text, tokens), vector in zip(items, vectors):
                await conn.execute(
                    """INSERT INTO travel_notes_chunks (note_id,chunk_idx,city,content,content_tokens,place_ids,embedding)
                    VALUES ($1,$2,$3,$4,$5,'{}',$6::vector)
                    ON CONFLICT DO NOTHING""",
                    record["id"], idx, record["city"], text, tokens, str(vector),
                )
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(ingest(load_records(args.input)))
