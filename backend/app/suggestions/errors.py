from app.itineraries.errors import ItineraryDomainError


class SuggestionSetExpiredError(ItineraryDomainError):
    code = "SUGGESTION_SET_EXPIRED"
    status_code = 409


class SuggestionSetStaleError(ItineraryDomainError):
    code = "SUGGESTION_SET_STALE"
    status_code = 409


class SuggestionCandidateBlockedError(ItineraryDomainError):
    code = "SUGGESTION_CANDIDATE_HARD_BLOCKED"
    status_code = 422


class SuggestionCandidateEvidenceUnavailableError(ItineraryDomainError):
    code = "SUGGESTION_CANDIDATE_EVIDENCE_UNAVAILABLE"
    status_code = 409


class SuggestionEdgeConflictError(ItineraryDomainError):
    code = "SUGGESTION_INSERT_EDGE_CONFLICT"
    status_code = 409


class SuggestionProviderUnavailableError(ItineraryDomainError):
    code = "SUGGESTION_PROVIDER_UNAVAILABLE"
    status_code = 503
