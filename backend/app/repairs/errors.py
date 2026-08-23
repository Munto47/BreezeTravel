from app.itineraries.errors import ItineraryDomainError


class RepairNoFeasibleOptionError(ItineraryDomainError):
    code = "REPAIR_NO_FEASIBLE_OPTION"
    status_code = 422


class RepairStaleError(ItineraryDomainError):
    code = "AUDIT_INPUT_STALE"
    status_code = 409


class InvalidRepairDecisionError(ItineraryDomainError):
    code = "INVALID_REPAIR_REJECTION_REASON"
    status_code = 422


class UnverifiedCandidateRejectedError(ItineraryDomainError):
    code = "UNVERIFIED_CANDIDATE_REJECTED"
    status_code = 422
