from __future__ import annotations

from pathlib import Path

from app.config import Settings, get_settings
from app.trip_intake.extraction import (
    DeterministicTripIntakeExtractor,
    HybridTripIntakeExtractor,
    SchemaConstrainedTripIntakeExtractor,
    TripIntakeExtractor,
    UnavailableHybridTripIntakeExtractor,
)
from app.trip_intake.llm_client import DeepSeekJsonClient
from app.trip_intake.runtime_audit import AuditedTripIntakeExtractor


def build_trip_intake_extractor(settings: Settings | None = None) -> TripIntakeExtractor:
    config = settings or get_settings()
    if config.trip_intake_extractor_mode == "deterministic":
        return DeterministicTripIntakeExtractor()
    if not config.deepseek_api_key.strip():
        return UnavailableHybridTripIntakeExtractor(model_name=config.trip_intake_model)
    client = DeepSeekJsonClient(
        api_key=config.deepseek_api_key,
        base_url=config.deepseek_api_url,
        timeout_seconds=config.trip_intake_timeout_seconds,
        max_output_tokens=config.trip_intake_max_output_tokens,
    )
    extractor: TripIntakeExtractor = HybridTripIntakeExtractor(
        SchemaConstrainedTripIntakeExtractor(
            client,
            model_name=config.trip_intake_model,
        )
    )
    if config.trip_intake_runtime_ledger_path.strip():
        extractor = AuditedTripIntakeExtractor(
            extractor,
            ledger_path=Path(config.trip_intake_runtime_ledger_path),
            input_usd_per_million=config.trip_intake_input_usd_per_million,
            output_usd_per_million=config.trip_intake_output_usd_per_million,
            usd_cny=config.trip_intake_usd_cny,
        )
    return extractor
