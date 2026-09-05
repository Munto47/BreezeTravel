"""Keep operational failure evidence without retaining prompts or provider bodies."""
def safe_failure_binding(binding: dict | None) -> dict:
    allowed = {
        "provider", "model", "model_snapshot", "status", "reason", "external_calls",
        "external_call_count", "input_tokens", "output_tokens", "total_tokens", "latency_ms",
        "estimated_cost_cny", "estimated_cny", "repair_calls", "fallback", "fallback_used", "outcome",
        "repair_call_count", "deadline_ms", "max_output_tokens", "prompt_sha256", "schema_sha256",
    }
    result = {key: value for key, value in (binding or {}).items()
              if key in allowed and (value is None or isinstance(value, (str, int, float, bool)))}
    call_keys = {"attempt", "input_tokens", "output_tokens", "outcome", "latency_ms", "reported_model"}
    calls = (binding or {}).get("calls")
    if isinstance(calls, list):
        result["calls"] = []
        for call in calls[:2]:
            if not isinstance(call, dict):
                continue
            safe = {key: value for key, value in call.items()
                    if key in call_keys and (value is None or isinstance(value, (str, int, float, bool)))}
            issues = call.get("validation_errors")
            if isinstance(issues, list):
                # The adapter constructs these locations from fixed schema fields,
                # never from the source quote, model body or Pydantic input/context.
                safe["validation_errors"] = [
                    {key: item[key] for key in ("field", "category") if isinstance(item.get(key), str)}
                    for item in issues[:20] if isinstance(item, dict)
                ]
            result["calls"].append(safe)
    return result
