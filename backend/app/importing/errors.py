from app.itineraries.errors import ItineraryDomainError


class ImportParseFailedError(ItineraryDomainError):
    code = "IMPORT_PARSE_FAILED"
    status_code = 422


class DraftAmbiguousError(ItineraryDomainError):
    code = "DRAFT_AMBIGUOUS"
    status_code = 409


class PlaceNotFoundError(ItineraryDomainError):
    code = "PLACE_NOT_FOUND"
    status_code = 422


class InvalidImportStateError(ItineraryDomainError):
    code = "INVALID_IMPORT_STATE"
    status_code = 409


class ImportStateConflictError(ItineraryDomainError):
    code = "IMPORT_STATE_CONFLICT"
    status_code = 409


class ScreenshotBatchInvalidError(ItineraryDomainError):
    code = "SCREENSHOT_BATCH_INVALID"
    status_code = 422


class OcrProcessingError(ItineraryDomainError):
    code = "OCR_PROCESSING_FAILED"
    status_code = 503


class PrivacyBlockedError(ItineraryDomainError):
    code = "PRIVACY_BLOCKED"
    status_code = 500
