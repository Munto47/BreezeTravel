from app.itineraries.errors import ItineraryDomainError


class IdempotencyRequestInProgressError(ItineraryDomainError):
    code = "IDEMPOTENCY_REQUEST_IN_PROGRESS"
    status_code = 409


class IdempotencyLeaseLostError(ItineraryDomainError):
    code = "IDEMPOTENCY_LEASE_LOST"
    status_code = 409
