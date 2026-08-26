from __future__ import annotations

from app.config import Settings, get_settings
from app.trip_intake.extraction import (
    DeterministicTripIntakeExtractor,
    HybridTripIntakeExtractor,
    SchemaConstrainedTripIntakeExtractor,
    TripIntakeExtractor,
    UnavailableHybridTripIntakeExtractor,
)
from app.trip_intake.llm_client import DeepSeekJsonClient


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
    return HybridTripIntakeExtractor(
        SchemaConstrainedTripIntakeExtractor(
            client,
            model_name=config.trip_intake_model,
        )
    )
