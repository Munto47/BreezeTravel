from app.itineraries.errors import ItineraryDomainError


class AuditInputStaleError(ItineraryDomainError):
    code = "AUDIT_INPUT_STALE"
    status_code = 409


class AuditReportNotFoundError(ItineraryDomainError):
    code = "RESOURCE_NOT_FOUND"
    status_code = 404

