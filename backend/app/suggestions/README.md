# Recommendation event write boundary

Public clients can append only these interaction events:

- `POST .../candidates/{candidate_id}:preview`
- `POST .../candidates/{candidate_id}:dismiss` with a required `reason_code`
- `POST .../suggestion-sets/{suggestion_set_id}:line-completed`

All three require `Idempotency-Key` and current room membership. The request
selects only the frozen set/candidate and, for dismiss, a reason. The server
loads `session_id`, `workspace_id`, `context_hash`, `policy_version`,
`provider_snapshot_id`, original `rank_position`, current revision and
`occurred_at`; client copies of those fields are never authoritative.

`suggestion_failed` is not client-writable. The SuggestionSet create API
appends it from the provider-error path even when no set ID exists, retaining
the workspace, session and safe request context.

The public itinerary Undo endpoint recognizes a direct
`ACCEPT_SUGGESTION_CANDIDATE` lineage. For that case it creates the new
revision, advances the workspace pointer, stores the itinerary command,
appends `stop_undone`, and binds the Undo to the original `candidate_accepted`
event in one workspace mutation transaction/lock. The request contains no
set, candidate, rank, context, policy, provider snapshot or stop identity;
those fields are frozen from the accepted revision and immutable event chain.
`SuggestionSetService.record_stop_undone` remains only as a legacy internal
hook and is not used by public Undo.

## Authoritative direct-accept gate

The production ranked provider passes every visible candidate through
`SuggestionAuditGate`.  It runs the existing `AuditEngine`/`AuditRuleRegistry`
against a temporary candidate revision and a candidate EvidenceSnapshot; no
AuditReport is persisted.  The frozen candidate stores the task revision,
effective member-constraint revision set, evidence input hash, rule-set
version, slot-policy version and finding summary.  Acceptance rechecks those
tokens while the repository holds the workspace mutation lock.

`UNKNOWN` candidates and `BLOCKER`/`HIGH` `VIOLATED` candidates remain visible
as `INFEASIBLE` but cannot be accepted. `MEDIUM`/`LOW` violations remain frozen
as visible warnings and ranking inputs; they are not silently promoted to HARD.
Fewer than four real candidates is `PARTIAL` with typed shortage codes;
candidates are never invented to fill the UI.  Amap opening hours are used only
when the V5 business payload contains a non-empty operational value.
Reservation and accessibility facts are required only when the task/member
inputs make them applicable.  Community content, official route priors and
LLM output cannot instantiate `CandidateCurrentFact` and never prove current
identity, opening, route, booking or accessibility facts.
