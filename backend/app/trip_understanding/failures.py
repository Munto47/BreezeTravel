"""Keep operational failure evidence without retaining prompts or provider bodies."""
def safe_failure_binding(binding: dict | None) -> dict:
    allowed = {
        "provider", "model", "model_snapshot", "status", "reason", "external_calls",
        "external_call_count", "input_tokens", "output_tokens", "total_tokens", "latency_ms",
        "estimated_cost_cny", "estimated_cny", "repair_calls", "fallback", "fallback_used", "outcome",
        "repair_call_count", "deadline_ms", "max_output_tokens", "prompt_sha256", "schema_sha256",
    }
    return {key: value for key, value in (binding or {}).items()
            if key in allowed and (value is None or isinstance(value, (str, int, float, bool)))}
