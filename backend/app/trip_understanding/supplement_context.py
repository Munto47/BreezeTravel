"""Retained semantic context follows the source's existing crypto erasure."""
import base64

from app.trip_understanding.models import SourceSemanticPlan


def _purpose(envelope: bytes) -> str:
    # Source deletion removes this random nonce. Do not store a second copy.
    return "supplement-source-plan:" + base64.b64encode(envelope[:13]).decode("ascii")


def seal_source_plan(cipher, plan, *, source_id, source_hash, envelope):
    if not envelope or not isinstance(plan, SourceSemanticPlan):
        return {}
    encrypted = cipher.encrypt(plan.model_dump_json(), source_id=source_id, content_hash=source_hash,
        purpose=_purpose(bytes(envelope)))
    return {"retained_source_plan": base64.b64encode(encrypted).decode("ascii")}


def open_source_plan(cipher, proposal, *, source_id, source_hash, envelope):
    encoded = proposal.get("retained_source_plan")
    if not envelope or not isinstance(encoded, str):
        return None
    plan = SourceSemanticPlan.model_validate_json(cipher.decrypt(base64.b64decode(encoded, validate=True),
        source_id=source_id, content_hash=source_hash, purpose=_purpose(bytes(envelope))))
    if plan.source_hash != source_hash:
        raise ValueError("retained semantic context source differs")
    return plan
