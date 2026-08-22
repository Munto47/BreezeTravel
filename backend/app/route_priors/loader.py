from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from app.route_priors.models import (
    CityOfficialPriorStatus,
    CommunityRoutePrior,
    OfficialCandidateHint,
    OfficialPriorAvailability,
    OfficialPriorReference,
    PriorCandidateHint,
    PriorContribution,
    RoutePriorSignals,
)


_REQUIRED_PROHIBITED_CLAIMS = {
    "CURRENT_OPENING",
    "CURRENT_RESERVATION",
    "CURRENT_PRICE",
    "CURRENT_ACCESSIBILITY",
    "CURRENT_ROUTE_TIME",
    "CURRENT_POPULARITY",
    "CANONICAL_IDENTITY",
    "COORDINATES",
}
_RIGHTS_SOURCE_ID = "open-wikivoyage-reuse-policy-20260821"
_SUPPORTED_CITIES = {"北京", "上海", "杭州"}
_OFFICIAL_ALLOWED_USE = ["STRUCTURE", "EVAL_ONLY"]
_OFFICIAL_REQUIRED_PROHIBITED = {
    "official-beijing-route-library-20260821": {
        "current_opening",
        "current_reservation",
        "current_route_time",
        "current_popularity",
    },
    "official-shanghai-citywalk-20240616": {
        "current_poi_identity",
        "current_opening",
        "current_route_time",
        "current_popularity",
    },
}
_OFFICIAL_RAW_SNAPSHOT_FIELDS = {
    "official-beijing-route-library-20260821": {
        "page_title",
        "linked_detail_url",
        "linked_route_title",
        "linked_route_sequence",
        "linked_detail_remote_body_sha256",
    },
    "official-shanghai-citywalk-20240616": {"page_title", "published_at", "route_sequence"},
}


class RoutePriorIntegrityError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutePriorIntegrityError(f"ROUTE_PRIOR_ARCHIVE_UNREADABLE:{path}") from exc
    if not isinstance(value, dict):
        raise RoutePriorIntegrityError(f"ROUTE_PRIOR_ARCHIVE_NOT_OBJECT:{path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutePriorIntegrityError(f"ROUTE_PRIOR_REGISTRY_UNREADABLE:{path}") from exc
    if not all(isinstance(row, dict) for row in rows):
        raise RoutePriorIntegrityError("ROUTE_PRIOR_REGISTRY_ROW_NOT_OBJECT")
    return rows


class RoutePriorLoader:
    """Hash-verifying read-only projection of archived open-community priors."""

    def __init__(self, dataset_root: Path | None = None):
        self.dataset_root = dataset_root or Path(__file__).resolve().parents[2] / "eval_data" / "dual_entry_v1"
        self.registry_path = self.dataset_root / "source_registry.jsonl"

    def load_all(self) -> tuple[CommunityRoutePrior, ...]:
        registry_rows = _load_jsonl(self.registry_path)
        by_id = {row.get("source_document_id"): row for row in registry_rows}
        if len(by_id) != len(registry_rows):
            raise RoutePriorIntegrityError("ROUTE_PRIOR_SOURCE_ID_DUPLICATE")
        rights = by_id.get(_RIGHTS_SOURCE_ID)
        self._validate_rights_source(rights)

        priors: list[CommunityRoutePrior] = []
        for row in registry_rows:
            if row.get("source_kind") != "wikivoyage_community":
                continue
            priors.append(self._load_one(row, rights))
        return tuple(sorted(priors, key=lambda item: (item.city, item.source_document_id)))

    def for_city(self, city: str) -> tuple[CommunityRoutePrior, ...]:
        return tuple(prior for prior in self.load_all() if prior.city == city)

    def candidate_hints(self, city: str, anchor_query: str, *, limit: int = 6) -> tuple[PriorCandidateHint, ...]:
        """Return unresolved neighboring/diversity hints; never a canonical POI."""
        if limit < 1:
            return ()
        anchor = self._normalize(anchor_query)
        output: list[PriorCandidateHint] = []
        seen: set[str] = {anchor}
        for prior in self.for_city(city):
            route_neighbors: list[str] = []
            anchor_in_prior = False
            for sequence in prior.route_sequences:
                normalized = [self._normalize(item) for item in sequence.query_hints]
                if anchor not in normalized:
                    continue
                anchor_in_prior = True
                index = normalized.index(anchor)
                if index > 0:
                    route_neighbors.append(sequence.query_hints[index - 1])
                if index + 1 < len(sequence.query_hints):
                    route_neighbors.append(sequence.query_hints[index + 1])
            for query_hint in route_neighbors:
                key = self._normalize(query_hint)
                if key in seen:
                    continue
                seen.add(key)
                output.append(self._hint(prior, query_hint, {PriorContribution.ROUTE_ADJACENCY}, ("COMMUNITY_ROUTE_NEIGHBOR",)))
            if anchor_in_prior or anchor in {self._normalize(item) for item in prior.related_place_query_hints}:
                for query_hint in prior.related_place_query_hints:
                    key = self._normalize(query_hint)
                    if key in seen:
                        continue
                    seen.add(key)
                    output.append(
                        self._hint(
                            prior,
                            query_hint,
                            {PriorContribution.CONTENT_RELEVANCE, PriorContribution.DIVERSITY},
                            ("COMMUNITY_RELATED_PLACE", "PROVIDER_RESOLUTION_REQUIRED"),
                        )
                    )
            if len(output) >= limit:
                break
        return tuple(output[:limit])

    def signals_for_city(
        self,
        city: str,
        anchor_query: str,
        *,
        community_limit: int = 6,
        official_limit: int = 6,
    ) -> RoutePriorSignals:
        """Return community and official signals without merging their provenance."""
        self._validate_city(city)
        official_priors, official_status = self._load_official_city(city)
        official_hints = self._official_candidate_hints(
            official_priors,
            anchor_query,
            limit=official_limit,
        )
        return RoutePriorSignals(
            city=city,
            community_hints=self.candidate_hints(city, anchor_query, limit=community_limit),
            official_hints=official_hints,
            official_status=official_status,
        )

    def official_status(self, city: str) -> CityOfficialPriorStatus:
        """Read official evidence availability without returning inferred suggestions."""
        self._validate_city(city)
        _, status = self._load_official_city(city)
        return status

    def _load_official_city(
        self,
        city: str,
    ) -> tuple[tuple[tuple[OfficialPriorReference, tuple[tuple[str, ...], ...]], ...], CityOfficialPriorStatus]:
        registry_rows = _load_jsonl(self.registry_path)
        official_rows = [
            row
            for row in registry_rows
            if row.get("source_type") == "OFFICIAL_ROUTE" and row.get("city") == city
        ]
        source_ids = [str(row.get("source_document_id", "?")) for row in official_rows]
        if len(source_ids) != len(set(source_ids)):
            raise RoutePriorIntegrityError("OFFICIAL_PRIOR_SOURCE_ID_DUPLICATE")

        loaded: list[tuple[OfficialPriorReference, tuple[tuple[str, ...], ...]]] = []
        unavailable: list[str] = []
        for row in official_rows:
            if row.get("access_status") != "VERIFIED_ACCESSIBLE":
                unavailable.append(str(row.get("source_document_id", "?")))
                continue
            if not row.get("raw_archive_path") or not row.get("extract_archive_path"):
                unavailable.append(str(row.get("source_document_id", "?")))
                continue
            loaded.append(self._load_official_one(row))

        refs = tuple(item[0] for item in loaded)
        if refs:
            status = CityOfficialPriorStatus(
                city=city,
                availability=OfficialPriorAvailability.AVAILABLE,
                available_source_refs=refs,
                unavailable_source_ids=tuple(sorted(unavailable)),
                reason_code="VERIFIED_ARCHIVE_AVAILABLE",
            )
        else:
            status = CityOfficialPriorStatus(
                city=city,
                availability=OfficialPriorAvailability.UNAVAILABLE,
                unavailable_source_ids=tuple(sorted(source_ids)),
                reason_code="OFFICIAL_ARCHIVE_UNAVAILABLE",
            )
        return tuple(loaded), status

    def _load_official_one(
        self,
        row: dict[str, Any],
    ) -> tuple[OfficialPriorReference, tuple[tuple[str, ...], ...]]:
        source_id = str(row.get("source_document_id", "?"))
        required = (
            "canonical_url",
            "raw_hash",
            "extract_hash",
            "raw_archive_path",
            "extract_archive_path",
            "captured_at",
        )
        if any(not row.get(field) for field in required):
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_PROVENANCE_INCOMPLETE:{source_id}")
        if row.get("usage_modes") != _OFFICIAL_ALLOWED_USE:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_USAGE_FORBIDDEN:{source_id}")
        parsed_url = urlparse(str(row["canonical_url"]))
        if parsed_url.scheme != "https" or parsed_url.hostname != row.get("domain"):
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_CANONICAL_URL_INVALID:{source_id}")

        raw_path = self._resolve_archive(str(row["raw_archive_path"]), source_id)
        extract_path = self._resolve_archive(str(row["extract_archive_path"]), source_id)
        if _sha256(raw_path) != row["raw_hash"] or _sha256(extract_path) != row["extract_hash"]:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_ARCHIVE_HASH_MISMATCH:{source_id}")
        raw = _load_json(raw_path)
        extract = _load_json(extract_path)
        self._validate_official_archive_shape(raw, extract, source_id)
        if raw.get("source_document_id") != source_id or extract.get("source_document_id") != source_id:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_SOURCE_ID_MISMATCH:{source_id}")
        canonical_url = row["canonical_url"]
        if raw.get("canonical_url") != canonical_url or extract.get("canonical_url") != canonical_url:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_CANONICAL_URL_MISMATCH:{source_id}")
        if raw.get("captured_at") != row["captured_at"] or extract.get("captured_at") != row["captured_at"]:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_CAPTURE_TIME_MISMATCH:{source_id}")

        remote_body_hash = raw.get("http", {}).get("remote_body_sha256")
        if not isinstance(remote_body_hash, str) or len(remote_body_hash) != 64:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_REMOTE_BODY_HASH_MISSING:{source_id}")
        derivation = extract.get("derivation", {})
        if (
            derivation.get("raw_archive_path") != row["raw_archive_path"]
            or derivation.get("source_body_sha256") != remote_body_hash
        ):
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_DERIVATION_BROKEN:{source_id}")
        if extract.get("allowed_use") != _OFFICIAL_ALLOWED_USE:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_USAGE_FORBIDDEN:{source_id}")
        required_prohibited = _OFFICIAL_REQUIRED_PROHIBITED.get(source_id)
        if not required_prohibited or not required_prohibited <= set(extract.get("prohibited_claims", [])):
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_FACT_BOUNDARY_INCOMPLETE:{source_id}")

        sequences = self._official_sequences(raw, extract, source_id)
        reference = OfficialPriorReference(
            source_document_id=source_id,
            canonical_url=canonical_url,
            city=row["city"],
            raw_sha256=row["raw_hash"],
            extract_sha256=row["extract_hash"],
            source_body_sha256=remote_body_hash,
            captured_at=row["captured_at"],
            allowed_use=("STRUCTURE", "EVAL_ONLY"),
        )
        return reference, sequences

    @staticmethod
    def _validate_official_archive_shape(
        raw: dict[str, Any],
        extract: dict[str, Any],
        source_id: str,
    ) -> None:
        raw_fields = {
            "schema_version",
            "source_document_id",
            "canonical_url",
            "captured_at",
            "capture_method",
            "http",
            "robots",
            "copyright_observed",
            "archive_policy",
            "minimal_snapshot",
        }
        extract_fields = {
            "schema_version",
            "source_document_id",
            "canonical_url",
            "captured_at",
            "derivation",
            "structured_route_priors",
            "allowed_use",
            "prohibited_claims",
        }
        if set(raw) != raw_fields or set(extract) != extract_fields:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_SCHEMA_OVERREACH:{source_id}")
        if raw.get("schema_version") != "dual-entry-source-capture-v1" or extract.get("schema_version") != "dual-entry-source-extract-v1":
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_SCHEMA_VERSION_INVALID:{source_id}")
        snapshot_fields = _OFFICIAL_RAW_SNAPSHOT_FIELDS.get(source_id)
        if snapshot_fields is None or set(raw.get("minimal_snapshot", {})) != snapshot_fields:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_SNAPSHOT_OVERREACH:{source_id}")
        derivation = extract.get("derivation")
        if not isinstance(derivation, dict) or set(derivation) != {
            "raw_archive_path",
            "source_body_sha256",
            "capture_fields",
            "method",
        }:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_DERIVATION_SCHEMA_INVALID:{source_id}")
        routes = extract.get("structured_route_priors")
        if not isinstance(routes, list) or any(
            not isinstance(route, dict) or set(route) != {"route_id", "theme", "ordered_stops"}
            for route in routes
        ):
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_ROUTE_SCHEMA_OVERREACH:{source_id}")
        if raw.get("http", {}).get("status") != 200:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_HTTP_NOT_SUCCESS:{source_id}")

    @staticmethod
    def _official_sequences(
        raw: dict[str, Any],
        extract: dict[str, Any],
        source_id: str,
    ) -> tuple[tuple[str, ...], ...]:
        structured = extract.get("structured_route_priors")
        if not isinstance(structured, list) or not structured:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_ROUTE_STRUCTURE_MISSING:{source_id}")
        sequences: list[tuple[str, ...]] = []
        for route in structured:
            stops = route.get("ordered_stops") if isinstance(route, dict) else None
            if not isinstance(stops, list) or len(stops) < 2 or any(not isinstance(stop, str) or not stop.strip() for stop in stops):
                raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_ROUTE_STRUCTURE_INVALID:{source_id}")
            normalized = [RoutePriorLoader._normalize(stop) for stop in stops]
            if len(normalized) != len(set(normalized)):
                raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_ROUTE_DUPLICATE_HINT:{source_id}")
            sequences.append(tuple(stops))

        snapshot = raw.get("minimal_snapshot", {})
        if "linked_route_sequence" in snapshot:
            raw_sequences = [snapshot["linked_route_sequence"]]
        elif "route_sequence" in snapshot:
            raw_sequences = [snapshot["route_sequence"]]
        else:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_RAW_ROUTE_MISSING:{source_id}")
        if [list(sequence) for sequence in sequences] != raw_sequences:
            raise RoutePriorIntegrityError(f"OFFICIAL_PRIOR_MINIMAL_PROJECTION_MISMATCH:{source_id}")
        return tuple(sequences)

    def _official_candidate_hints(
        self,
        priors: tuple[tuple[OfficialPriorReference, tuple[tuple[str, ...], ...]], ...],
        anchor_query: str,
        *,
        limit: int,
    ) -> tuple[OfficialCandidateHint, ...]:
        if limit < 1:
            return ()
        anchor = self._normalize(anchor_query)
        output: list[OfficialCandidateHint] = []
        seen: set[str] = {anchor}
        for reference, sequences in priors:
            for sequence in sequences:
                normalized = [self._normalize(item) for item in sequence]
                if anchor not in normalized:
                    continue
                index = normalized.index(anchor)
                neighbors = []
                if index > 0:
                    neighbors.append(sequence[index - 1])
                if index + 1 < len(sequence):
                    neighbors.append(sequence[index + 1])
                for query_hint in neighbors:
                    key = self._normalize(query_hint)
                    if key in seen:
                        continue
                    seen.add(key)
                    output.append(
                        OfficialCandidateHint(
                            query_hint=query_hint,
                            city=reference.city,
                            contributions=frozenset({PriorContribution.ROUTE_ADJACENCY}),
                            explanation_codes=("OFFICIAL_ROUTE_NEIGHBOR", "PROVIDER_RESOLUTION_REQUIRED"),
                            official_prior_refs=(reference,),
                        )
                    )
        return tuple(output[:limit])

    @staticmethod
    def _validate_city(city: str) -> None:
        if city not in _SUPPORTED_CITIES:
            raise ValueError(f"unsupported route-prior city: {city}")

    def _load_one(self, row: dict[str, Any], rights: dict[str, Any]) -> CommunityRoutePrior:
        source_id = str(row.get("source_document_id", "?"))
        if row.get("source_type") != "OPEN_DATA" or "FACT" in row.get("usage_modes", []):
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_FACT_USAGE_FORBIDDEN:{source_id}")
        required_registry = (
            "raw_hash",
            "extract_hash",
            "raw_archive_path",
            "extract_archive_path",
            "source_revision",
            "revision_url",
            "content_hash",
            "attribution",
            "license_spdx",
            "license_url",
            "rights_source_document_id",
        )
        if any(not row.get(field) for field in required_registry):
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_PROVENANCE_INCOMPLETE:{source_id}")
        if row["rights_source_document_id"] != rights["source_document_id"]:
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_RIGHTS_SOURCE_MISMATCH:{source_id}")
        if row["license_spdx"] != "CC-BY-SA-4.0" or row["license_url"] != rights["license_url"]:
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_LICENSE_MISMATCH:{source_id}")

        raw_path = self._resolve_archive(row["raw_archive_path"], source_id)
        extract_path = self._resolve_archive(row["extract_archive_path"], source_id)
        if _sha256(raw_path) != row["raw_hash"] or _sha256(extract_path) != row["extract_hash"]:
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_ARCHIVE_HASH_MISMATCH:{source_id}")
        raw = _load_json(raw_path)
        extract = _load_json(extract_path)
        if raw.get("source_document_id") != source_id or extract.get("source_document_id") != source_id:
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_SOURCE_ID_MISMATCH:{source_id}")
        if raw.get("canonical_url") != row["canonical_url"] or extract.get("canonical_url") != row["canonical_url"]:
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_CANONICAL_URL_MISMATCH:{source_id}")
        content_hash = raw.get("http", {}).get("remote_body_sha256")
        if content_hash != row["content_hash"] or extract.get("content_sha256") != content_hash:
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_CONTENT_HASH_MISMATCH:{source_id}")
        derivation = extract.get("derivation", {})
        if derivation.get("raw_archive_path") != row["raw_archive_path"] or derivation.get("source_body_sha256") != content_hash:
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_DERIVATION_BROKEN:{source_id}")
        for field in ("source_revision", "revision_url"):
            if str(extract.get(field)) != str(row[field]) or str(raw.get("revision", {}).get(field)) != str(row[field]):
                raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_REVISION_MISMATCH:{source_id}:{field}")
        licence = extract.get("licence", {})
        if (
            licence.get("spdx") != row["license_spdx"]
            or licence.get("url") != row["license_url"]
            or licence.get("attribution") != row["attribution"]
            or licence.get("rights_source_document_id") != row["rights_source_document_id"]
        ):
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_ATTRIBUTION_MISMATCH:{source_id}")
        if not set(extract.get("allowed_use", [])) <= set(row.get("usage_modes", [])):
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_USAGE_EXPANSION:{source_id}")
        if not _REQUIRED_PROHIBITED_CLAIMS <= set(extract.get("prohibited_claims", [])):
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_FACT_BOUNDARY_INCOMPLETE:{source_id}")
        snapshot = raw.get("minimal_snapshot", {})
        for field in (
            "route_sequences",
            "related_place_query_hints",
            "experience_tags",
            "season_hints",
            "audience_hints",
        ):
            if extract.get(field) != snapshot.get(field):
                raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_MINIMAL_PROJECTION_MISMATCH:{source_id}:{field}")
        try:
            return CommunityRoutePrior.model_validate(extract)
        except ValidationError as exc:
            raise RoutePriorIntegrityError(f"COMMUNITY_PRIOR_SCHEMA_INVALID:{source_id}:{exc}") from exc

    def _validate_rights_source(self, rights: dict[str, Any] | None) -> None:
        if not rights:
            raise RoutePriorIntegrityError("WIKIVOYAGE_RIGHTS_SOURCE_MISSING")
        required = (
            "raw_hash",
            "extract_hash",
            "raw_archive_path",
            "extract_archive_path",
            "source_revision",
            "revision_url",
            "content_hash",
            "attribution",
            "license_spdx",
            "license_url",
        )
        if any(not rights.get(field) for field in required):
            raise RoutePriorIntegrityError("WIKIVOYAGE_RIGHTS_SOURCE_INCOMPLETE")
        if rights["license_spdx"] != "CC-BY-SA-4.0":
            raise RoutePriorIntegrityError("WIKIVOYAGE_RIGHTS_LICENSE_UNSUPPORTED")
        for kind in ("raw", "extract"):
            path = self._resolve_archive(rights[f"{kind}_archive_path"], rights["source_document_id"])
            if _sha256(path) != rights[f"{kind}_hash"]:
                raise RoutePriorIntegrityError("WIKIVOYAGE_RIGHTS_ARCHIVE_HASH_MISMATCH")
        raw = _load_json(self._resolve_archive(rights["raw_archive_path"], rights["source_document_id"]))
        if raw.get("http", {}).get("remote_body_sha256") != rights["content_hash"]:
            raise RoutePriorIntegrityError("WIKIVOYAGE_RIGHTS_CONTENT_HASH_MISMATCH")

    def _resolve_archive(self, value: str, source_id: str) -> Path:
        path = (self.dataset_root / value).resolve()
        try:
            path.relative_to(self.dataset_root.resolve())
        except ValueError as exc:
            raise RoutePriorIntegrityError(f"ROUTE_PRIOR_ARCHIVE_PATH_ESCAPE:{source_id}") from exc
        if not path.is_file():
            raise RoutePriorIntegrityError(f"ROUTE_PRIOR_ARCHIVE_MISSING:{source_id}:{value}")
        return path

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.split()).casefold()

    @staticmethod
    def _hint(
        prior: CommunityRoutePrior,
        query_hint: str,
        contributions: set[PriorContribution],
        explanation_codes: tuple[str, ...],
    ) -> PriorCandidateHint:
        return PriorCandidateHint(
            query_hint=query_hint,
            city=prior.city,
            contributions=frozenset(contributions),
            explanation_codes=explanation_codes,
            source_document_id=prior.source_document_id,
            source_revision=prior.source_revision,
            revision_url=prior.revision_url,
            content_sha256=prior.content_sha256,
            attribution=prior.licence.attribution,
            license_spdx=prior.licence.spdx,
        )
