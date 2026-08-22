from __future__ import annotations

from app.members.models import (
    MemberConstraintDraft,
    MemberConstraintWriteResult,
    TravelerProfile,
)
from app.members.repositories import MemberConstraintRepository


class MemberConstraintService:
    def __init__(self, repository: MemberConstraintRepository):
        self.repository = repository

    async def save_profile(self, profile: TravelerProfile) -> TravelerProfile:
        return await self.repository.save_profile(profile)

    async def write_constraint(
        self,
        workspace_id: str,
        draft: MemberConstraintDraft,
        *,
        expected_base_revision: int,
    ) -> MemberConstraintWriteResult:
        constraint, stale_report_id = await self.repository.append_constraint(
            workspace_id,
            draft,
            expected_base_revision=expected_base_revision,
        )
        return MemberConstraintWriteResult(
            constraint=constraint,
            previous_workspace_revision=expected_base_revision,
            current_workspace_revision=constraint.revision,
            stale_report_id=stale_report_id,
        )
