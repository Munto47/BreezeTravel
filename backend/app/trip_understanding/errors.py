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


class ScreenshotBatchNotFoundError(TripUnderstandingError):
    pass


class ScreenshotBatchExpiredError(TripUnderstandingError):
    pass


class ScreenshotBatchAlreadyUsedError(TripUnderstandingError):
    pass


class ScreenshotBatchNotReadyError(TripUnderstandingError):
    pass


class ScreenshotBatchUnusableError(TripUnderstandingError):
    pass


class RevisionConflictError(TripUnderstandingError):
    pass


class ResourceNotReadyError(TripUnderstandingError):
    pass


class CommandTargetChangedError(TripUnderstandingError):
    pass


class ExpectedProviderUnavailableError(TripUnderstandingError):
    """Typed, redacted failure that product fallbacks may safely handle."""

    def __init__(
        self,
        category: str,
        *,
        provider_binding: dict[str, object],
        external_call_count: int,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.provider_binding = provider_binding
        self.external_call_count = external_call_count


class InferenceProviderUnavailableError(ExpectedProviderUnavailableError):
    pass


class PlaceProviderUnavailableError(ExpectedProviderUnavailableError):
    pass


class RouteProviderUnavailableError(ExpectedProviderUnavailableError):
    pass
