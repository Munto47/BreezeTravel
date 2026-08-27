class TripUnderstandingError(Exception):
    """Base error for the v3 understanding boundary."""


class CapabilityExpiredError(TripUnderstandingError):
    pass


class IdempotencyConflictError(TripUnderstandingError):
    pass


class IdempotencyInProgressError(TripUnderstandingError):
    pass


class ConcurrentJobLimitError(TripUnderstandingError):
    pass


class ResourceNotFoundError(TripUnderstandingError):
    pass


class ResourceGoneError(TripUnderstandingError):
    pass


class ResourceAccessDeniedError(TripUnderstandingError):
    pass


class JobLeaseLostError(TripUnderstandingError):
    pass


class SourceUnavailableError(TripUnderstandingError):
    pass


class RevisionConflictError(TripUnderstandingError):
    pass


class ResourceNotReadyError(TripUnderstandingError):
    pass


class CommandTargetChangedError(TripUnderstandingError):
    pass
