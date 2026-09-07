"""Collect public official name lists into a reviewable, inactive city pack.

Usage: python -m scripts.collect_city_knowledge --source gz-attractions --output /new/path.jsonl
No login, cookies, POI identities, contact details or full articles are retained.
The command refuses to overwrite files and never edits the active manifest.
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import date
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.trip_understanding.city_knowledge import KNOWLEDGE_ROOT


class PublicPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.labels, self.text = [], [], []
        self._row, self._cell, self._label = None, None, None
        self._ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._ignore += 1
        if tag == "tr":
            self._row = []
        if tag in {"td", "th"} and self._row is not None:
            self._cell = []
        if tag in {"a", "h2", "h3", "h4", "h5"}:
            self._label = []

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._ignore = max(0, self._ignore - 1)
        if tag in {"td", "th"} and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        if tag in {"a", "h2", "h3", "h4", "h5"} and self._label is not None:
            self.labels.append("".join(self._label).strip())
            self._label = None

    def handle_data(self, data):
        if self._ignore:
            return
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)
        if self._label is not None:
            self._label.append(data)


def fetch(source: dict) -> PublicPageParser:
    if source.get("parser") == "manual_review":
        raise ValueError("This source requires public article/PDF review; automatic collection is not configured")
    url = source["url"]
    host = urlsplit(url).hostname or ""
    if not host.endswith(".gov.cn"):
        raise ValueError("Collector permits explicit official public sources only")
    request = Request(url, headers={"User-Agent": "BreezeTravel-public-name-review/1.0"})
    with urlopen(request, timeout=20) as response:
        if not (urlsplit(response.url).hostname or "").endswith(".gov.cn"):
            raise ValueError("Unexpected source redirect")
        payload = response.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise ValueError("Public source exceeds bounded download size")
    parser = PublicPageParser()
    parser.feed(payload.decode("utf-8"))
    return parser


def entity_id(city: str, kind: str, name: str) -> str:
    digest = hashlib.sha256(f"{city}|{kind}|{name}".encode()).hexdigest()[:20]
    return f"city-v1:{digest}"


def record(source: dict, name: str, *, evidence: str, district: str | None = None,
           kind: str | None = None, uses: list[str] | None = None) -> dict:
    kind = kind or source.get("kind", "poi")
    return {"id": entity_id(source["city"], kind, name), "city": source["city"],
        "canonical_name": name, "kind": kind, "category": "area" if kind in {"commercial_area", "stay_area"} else source.get("category", "attraction"),
        "aliases": [], "district": district, "review_status": "name_verified",
        "sources": [{"url": source["url"], "title": source["title"], "publisher": source["publisher"],
            "retrieved_at": date.today().isoformat(), "evidence": evidence}],
        "relations": [], "uses": uses or []}


def extract(source: dict, page: PublicPageParser) -> list[dict]:
    records = []
    if source["parser"] == "reviewed_names":
        text = re.sub(r"\s+", "", "".join(page.text))
        for spec in source["names"]:
            name = spec if isinstance(spec, str) else spec["name"]
            spec = {} if isinstance(spec, str) else spec
            if name not in text:
                raise ValueError(f"Reviewed name absent from official source: {name}")
            records.append(record(source, name, evidence="Exact name in official article; no popularity, opening or travel-time claim.",
                district=spec.get("district"), kind=spec.get("kind"), uses=spec.get("uses")))
    elif source["parser"] == "parks_labels":
        for label in page.labels:
            name = re.sub(r"\s+", "", label)
            if (3 <= len(name) <= 24 and (name.endswith("公园") or name.endswith("风景名胜区"))
                    and name.count("公园") <= 1 and name not in {"特色公园", "自然公园", "城市公园", "社区公园"}
                    and not any(s in name for s in ("深圳公园", "建设", "规划", "探访", "走进", "公路", "街道"))):
                records.append(record(source, name, evidence="Named park entry in the official park directory."))
    elif source["parser"] == "table":
        for row in page.rows:
            cells = [re.sub(r"\s+", "", cell) for cell in row]
            if not cells or not re.fullmatch(r"\d{1,4}", cells[0]):
                continue
            name_col = source["name_column"]
            if len(cells) <= name_col:
                continue
            name = cells[name_col]
            if not name or len(name) > 100:
                raise ValueError("Unexpected source table shape")
            district_col = source.get("district_column")
            district = cells[district_col] if district_col is not None else None
            if district and not district.endswith(("区", "县", "市")):
                district += "区"
            records.append(record(source, name, district=district, evidence=f"Official directory table row {cells[0]}, name column; current POI and operation not verified."))
    else:
        raise ValueError("Unknown explicit collection recipe")
    unique = {}
    for item in records:
        unique.setdefault(item["id"], item)
    if not unique:
        raise ValueError("Source produced no names; active data remains untouched")
    return list(unique.values())


def main() -> None:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--source", required=True)
    args.add_argument("--output", required=True, type=Path)
    options = args.parse_args()
    catalog = json.loads((KNOWLEDGE_ROOT / "sources.json").read_text(encoding="utf-8"))
    source = next(s for s in catalog if s["id"] == options.source)
    records = extract(source, fetch(source))
    options.output.parent.mkdir(parents=True, exist_ok=True)
    with options.output.open("x", encoding="utf-8", newline="\n") as stream:
        for item in records:
            stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps({"source": options.source, "city": source["city"], "name_records": len(records),
        "current_poi_verified": False, "active_manifest_modified": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
