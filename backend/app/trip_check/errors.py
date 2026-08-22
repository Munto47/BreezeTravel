from app.itineraries.errors import ItineraryDomainError


class TripBriefRevisionConflictError(ItineraryDomainError):
    code = "TRIP_BRIEF_REVISION_CONFLICT"
    status_code = 409


class TripBriefAlreadyConfirmedError(ItineraryDomainError):
    code = "TRIP_BRIEF_ALREADY_CONFIRMED"
    status_code = 409


class TripBriefIncompleteError(ItineraryDomainError):
    code = "TRIP_BRIEF_INCOMPLETE"
    status_code = 422


class RunConfigMismatchError(ItineraryDomainError):
    code = "RUN_CONFIG_MISMATCH"
    status_code = 409
