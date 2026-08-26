from __future__ import annotations

from typing import Any


class ItineraryDomainError(Exception):
    code = "ITINERARY_ERROR"
    status_code = 422

    def __init__(self, message: str, *, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.context = context or {}

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.context}


class ResourceNotFound(ItineraryDomainError):
    code = "RESOURCE_NOT_FOUND"
    status_code = 404


class ResourceScopeDenied(ItineraryDomainError):
    code = "RESOURCE_SCOPE_DENIED"
    status_code = 403


class RevisionConflictError(ItineraryDomainError):
    code = "ITINERARY_REVISION_CONFLICT"
    status_code = 409


class CurrentAuditRequiredError(ItineraryDomainError):
    """Raised when an irreversible workspace confirmation has no valid full audit."""

    code = "CURRENT_AUDIT_REQUIRED"
    status_code = 409


class IdempotencyKeyReusedError(ItineraryDomainError):
    code = "IDEMPOTENCY_KEY_REUSED"
    status_code = 409


class LockedCommitmentError(ItineraryDomainError):
    code = "PATCH_BREAKS_LOCKED_COMMITMENT"
    status_code = 422


class InvalidEditCommandError(ItineraryDomainError):
    code = "INVALID_ITINERARY_EDIT_COMMAND"
    status_code = 422


class TipsNotEligibleError(ItineraryDomainError):
    code = "TIPS_NOT_ELIGIBLE"
    status_code = 409


class TipsInputConflictError(ItineraryDomainError):
    code = "TIPS_ALREADY_GENERATED_WITH_DIFFERENT_INPUT"
    status_code = 409
