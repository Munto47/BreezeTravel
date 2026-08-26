"""Build a reproducible, licence-allowlisted public travel corpus.

Only Wikimedia APIs are queried: Wikivoyage text is CC BY-SA 4.0 and Wikidata
structured facts are CC0.  The script deliberately has no generic URL input,
so a future change cannot silently become a crawler for restricted platforms.

Run from ``backend``:
    python -m scripts.build_public_corpus --output data/generated/public_sources.jsonl
"""

import argparse
import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp


# The Action API is deliberately used instead of the REST summary endpoint.
# It returns the actual, immutable page revision that our attribution URL names;
# a summary can change independently and is too short for travel retrieval.
WIKIVOYAGE_API = "https://en.wikivoyage.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY_DATA = "https://www.wikidata.org/wiki/Special:EntityData/"
WIKIVOYAGE_LICENSE = "CC BY-SA 4.0"
WIKIDATA_LICENSE = "CC0 1.0"
_MIN_CONTENT_CHARS = 240
# One request per second is below Wikimedia's interactive API guidance while
# keeping a 12-record refresh bounded enough for CI/manual recovery.
_REQUEST_INTERVAL_SECONDS = 1.0
_last_request_at = 0.0


class WikimediaRateLimited(RuntimeError):
    """A source-level rate limit; callers may choose an official fallback."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_id(prefix: str, city: str, revision: str) -> str:
    suffix = hashlib.sha256(f"{prefix}:{city}:{revision}".encode()).hexdigest()[:16]
    return f"{prefix}-{suffix}"


def _clean_wikitext(value: str) -> str:
    """Conservative plaintext extraction without pretending templates are facts."""
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    value = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[https?://[^\s\]]+\s*([^\]]*)\]", r"\1", value)
    value = re.sub(r"={2,}[^=]+={2,}", "", value)
    value = re.sub(r"'{2,}", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict[str, Any], *, retries: int = 1) -> dict:
    global _last_request_at
    wait = _REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
    if wait > 0:
        await asyncio.sleep(wait)
    for attempt in range(retries):
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as response:
            _last_request_at = time.monotonic()
            if response.status != 429:
                response.raise_for_status()
                return await response.json()
            if attempt + 1 == retries:
                raise WikimediaRateLimited("Wikimedia API rate limited")
            # A failed refresh remains visible in the manifest instead of
            # falling back to stale or unaudited content.  Callers normally
            # make one attempt, so a CI run cannot leave an orphan process
            # sleeping through a long Retry-After header.
            retry_after = response.headers.get("Retry-After")
            # Honour an explicit server instruction.  Without one, retry
            # quickly but exponentially; the failed source is then written to
            # the manifest and blocks import/publish rather than hanging an
            # operator for minutes on every source.
            await asyncio.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1))
    raise WikimediaRateLimited("Wikimedia API rate limited after bounded retries")


async def fetch_wikivoyage_revision(session: aiohttp.ClientSession, city: str, title: str) -> dict:
    """Fetch one named Wikivoyage page and retain its fixed revision ID."""
    payload = await _get_json(session, WIKIVOYAGE_API, {
        "action": "query", "format": "json", "formatversion": "2",
        "prop": "revisions", "titles": title, "rvprop": "ids|timestamp|content",
        "rvslots": "main", "redirects": "1",
    })
    page = payload.get("query", {}).get("pages", [{}])[0]
    revision = (page.get("revisions") or [{}])[0]
    raw_content = revision.get("slots", {}).get("main", {}).get("content", "")
    text = _clean_wikitext(raw_content)
    if len(text) < _MIN_CONTENT_CHARS:
        raise ValueError(f"wikivoyage page too short after normalization: {title}")
    revision_id = str(revision["revid"])
    canonical_title = page["title"]
    return {
        "id": _stable_id("wikivoyage", city, revision_id), "title": f"Wikivoyage: {canonical_title}",
        "city": city, "content": text,
        "source_url": f"https://en.wikivoyage.org/w/index.php?title={canonical_title.replace(' ', '_')}&oldid={revision_id}",
        "source_published_at": revision.get("timestamp"), "source_retrieved_at": _utc_now(),
        "source_license": WIKIVOYAGE_LICENSE, "source_revision": revision_id,
        "source_attribution": f"Wikivoyage contributors, {canonical_title}, revision {revision_id}",
        "tags": ["public", "wikivoyage", "travel-guide"], "corpus_kind": "public",
    }


async def fetch_wikidata(session: aiohttp.ClientSession, city: str, qid: str) -> dict:
    try:
        payload = await _get_json(session, WIKIDATA_API, {
            "action": "wbgetentities", "format": "json", "ids": qid,
            "props": "labels|descriptions|aliases|claims|info", "languages": "zh|en",
        }, retries=1)
    except WikimediaRateLimited:
        # Same Wikimedia project, a separate official representation.  It is
        # deliberately not a mirror or third-party cache and its revision is
        # still captured below.  This makes an Action API burst recoverable.
        payload = await _get_json(session, f"{WIKIDATA_ENTITY_DATA}{qid}.json", {}, retries=1)
    entity = payload["entities"][qid]
    labels = entity.get("labels", {})
    descriptions = entity.get("descriptions", {})
    label = labels.get("zh", labels.get("en", {})).get("value", qid)
    description = descriptions.get("zh", descriptions.get("en", {})).get("value", "")
    aliases = [item["value"] for language in ("zh", "en") for item in entity.get("aliases", {}).get(language, [])[:2]]
    claims = entity.get("claims", {})

    def claim_values(property_id: str) -> list[str]:
        values: list[str] = []
        for claim in claims.get(property_id, [])[:3]:
            datavalue = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(datavalue, dict) and "id" in datavalue:
                values.append(datavalue["id"])
            elif isinstance(datavalue, dict) and {"latitude", "longitude"} <= datavalue.keys():
                values.append(f"{datavalue['latitude']:.5f},{datavalue['longitude']:.5f}")
            elif isinstance(datavalue, (str, int, float)):
                values.append(str(datavalue))
        return values

    structured = [
        f"Wikidata ID {qid}",
        *( [f"坐标 {', '.join(claim_values('P625'))}"] if claim_values("P625") else [] ),
        *( [f"实例 {', '.join(claim_values('P31'))}"] if claim_values("P31") else [] ),
        *( [f"所在行政区 {', '.join(claim_values('P131'))}"] if claim_values("P131") else [] ),
        *( [f"国家 {', '.join(claim_values('P17'))}"] if claim_values("P17") else [] ),
    ]
    content = "；".join(part for part in [f"{city}结构化地点事实", label, description, *aliases, *structured] if part)
    revision = str(entity.get("lastrevid", qid))
    return {
        "id": _stable_id("wikidata", city, revision), "title": f"Wikidata: {label}", "city": city,
        "content": f"{city}地点结构化事实：{content}。", "source_url": f"https://www.wikidata.org/wiki/{qid}",
        "source_retrieved_at": _utc_now(), "source_license": WIKIDATA_LICENSE, "source_revision": revision,
        "source_attribution": f"Wikidata contributors, {qid}", "tags": ["public", "wikidata", "structured-facts"], "corpus_kind": "public",
    }


async def build_records(source_config: dict) -> tuple[list[dict], list[dict]]:
    records: list[dict] = []
    failures: list[dict] = []
    # Wikimedia requests a meaningful contact route for automated clients.
    # Allow an operator to override it without turning this into a generic
    # crawler; the allowlisted endpoint set stays fixed in this module.
    headers = {"User-Agent": "BreezeTravel-public-corpus/1.1 (educational reproducible RAG; contact: breezetravel@example.invalid)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for item in source_config["cities"]:
            city = item["city"]
            try:
                records.append(await fetch_wikivoyage_revision(session, city, item["wikivoyage_title"]))
            except Exception as exc:
                failures.append({"city": city, "source": "wikivoyage", "reason": str(exc)})
            for qid in item["wikidata_qids"]:
                try:
                    records.append(await fetch_wikidata(session, city, qid))
                except Exception as exc:
                    failures.append({"city": city, "source": "wikidata", "reason": str(exc)})
    return records, failures


def write_outputs(records: list[dict], failures: list[dict], output: Path, manifest: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    normalized = []
    for item in records:
        item = dict(item)
        item["source_content_hash"] = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
        normalized.append(item)
    output.write_text("\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in normalized) + "\n", encoding="utf-8")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest.write_text(json.dumps({
        "schema_version": "1.0", "generated_at": _utc_now(), "records": len(records), "failures": failures,
        "output": str(output).replace("\\", "/"), "sha256": digest,
        "licences": {"wikivoyage": WIKIVOYAGE_LICENSE, "wikidata": WIKIDATA_LICENSE},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, default=Path("data/public_corpus_sources.json"))
    parser.add_argument("--output", type=Path, default=Path("data/generated/public_sources.jsonl"))
    parser.add_argument("--manifest", type=Path, default=Path("evidence/corpus/latest.json"))
    args = parser.parse_args()
    config = json.loads(args.sources.read_text(encoding="utf-8"))
    records, failures = asyncio.run(build_records(config))
    write_outputs(records, failures, args.output, args.manifest)
    expected_count = sum(1 + len(city["wikidata_qids"]) for city in config["cities"])
    if failures or len(records) != expected_count:
        raise SystemExit("Public corpus is incomplete; inspect the generated manifest before import.")


if __name__ == "__main__":
    main()
