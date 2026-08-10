from app.constraints.base import RuleContext
from app.constraints.rules._utils import find_constraints
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class CollaborationSnapshotRule:
    rule_id = "collaboration_snapshot"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        checks = []
        for constraint in find_constraints(context.task_spec, "preserve_majority_voted"):
            places = [slot.place or {} for day in context.itinerary.days for slot in day.slots]
            explicit = [place for place in places if place.get("majority_voted") is True]
            vote_snapshots = [place.get("votedBy") or place.get("voted_by") for place in places]
            if explicit:
                status = ConstraintStatus.SATISFIED
                reason = "MAJORITY_VOTED_PLACE_PRESENT"
                message = "多数投票地点已保留"
            elif any(votes for votes in vote_snapshots):
                status = ConstraintStatus.UNKNOWN
                reason = "COMPLETE_VOTE_SNAPSHOT_MISSING"
                message = "只有行程内投票数据，缺少完整候选快照，无法证明最高票地点已保留"
            else:
                status = ConstraintStatus.UNKNOWN
                reason = "VOTE_SNAPSHOT_MISSING"
                message = "缺少投票快照，无法验证多数投票地点是否保留"
            checks.append(ConstraintCheck(
                constraint_id=constraint.id,
                status=status,
                reason_code=reason,
                message=message,
                repairable=False,
            ))
        return checks
