"""Keep operational failure evidence without retaining prompts or provider bodies."""
from app.trip_understanding.models import MAX_TRIP_ACTIVITIES


INPUT_CAPACITY_EXCEEDED = "INPUT_CAPACITY_EXCEEDED"
CAPACITY_EXCEEDED_MESSAGE = f"这次整理的内容超过 {MAX_TRIP_ACTIVITIES} 项上限，请分成多份行程后再试。"
INPUT_DAY_CAPACITY_EXCEEDED = "INPUT_DAY_CAPACITY_EXCEEDED"
DAY_CAPACITY_EXCEEDED_MESSAGE = "这次整理的行程超过 14 天上限，请分成多份行程后再试。"


def public_failure_message(category: str) -> str:
    if category == INPUT_CAPACITY_EXCEEDED:
        return CAPACITY_EXCEEDED_MESSAGE
    if category == INPUT_DAY_CAPACITY_EXCEEDED:
        return DAY_CAPACITY_EXCEEDED_MESSAGE
    return "这次没有整理完成，可以重新尝试"


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
