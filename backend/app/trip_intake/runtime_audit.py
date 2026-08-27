from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from app.trip_intake.extraction import ExtractionOutcome, TripIntakeExtractor
from app.trip_intake.models import IntakeSource


class AuditedTripIntakeExtractor:
    """Append privacy-safe hybrid call receipts to an opt-in local JSONL ledger."""

    def __init__(
        self,
        delegate: TripIntakeExtractor,
        *,
        ledger_path: Path,
        input_usd_per_million: float,
        output_usd_per_million: float,
        usd_cny: float,
    ) -> None:
        self.delegate = delegate
        self.ledger_path = ledger_path
        self.input_usd_per_million = input_usd_per_million
        self.output_usd_per_million = output_usd_per_million
        self.usd_cny = usd_cny

    async def extract(self, sources: list[IntakeSource]) -> ExtractionOutcome:
        outcome = await self.delegate.extract(sources)
        receipt = outcome.runtime_receipt
        if receipt is None:
            raise ValueError("audited Trip Intake extraction requires a runtime receipt")
        estimated_cost_cny = (
            (
                receipt.input_tokens * self.input_usd_per_million
                + receipt.output_tokens * self.output_usd_per_million
            )
            / 1_000_000
            * self.usd_cny
        )
        row = {
            "schema_version": "trip-intake-runtime-call-receipt-v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "source_sha256": [sha256(source.text.encode("utf-8")).hexdigest() for source in sources],
            "parser_binding": outcome.parser_binding.model_dump(mode="json"),
            "status": outcome.status.value,
            "requested_model": receipt.requested_model,
            "actual_model": receipt.actual_model,
            "input_tokens": receipt.input_tokens,
            "output_tokens": receipt.output_tokens,
            "latency_ms": round(receipt.latency_ms, 3),
            "fallback_used": receipt.fallback_used,
            "error_category": receipt.error_category,
            "error_detail": receipt.error_detail,
            "estimated_cost_cny": round(estimated_cost_cny, 8),
            "pricing": {
                "input_usd_per_million": self.input_usd_per_million,
                "output_usd_per_million": self.output_usd_per_million,
                "usd_cny": self.usd_cny,
            },
        }
        self._append(row)
        return outcome

    def _append(self, row: dict[str, object]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            self.ledger_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
