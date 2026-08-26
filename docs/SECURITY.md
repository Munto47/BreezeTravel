# Security and governance boundaries

## Identity and room authorization

HTTP identity is derived from the verified bearer token, never from a request
body `user_id`. Room, task, edit, optimize, memory and persisted-place operations
check membership before reading or writing room-scoped data. Demo/test profiles
retain an explicit local compatibility path; public profile fails closed.

The room-token endpoint issues a short-lived JWT containing `sub`, `room_id`,
`iat`, `exp` and `jti`. The Yjs server validates signature, expiry and exact room
binding before accepting a WebSocket, so a valid token for room A cannot subscribe
to room B or its awareness stream.

## Memory governance

Memories include source, confidence, lifecycle state and timestamps. Only stable
preferences above the confidence threshold are injected. Users can list, correct,
opt out and delete their memories; TTL expiry and deletion are enforced in the
governance layer and database migration 008.

## Prompt and output boundary

Retrieved notes and tool outputs are data, not instructions. Patterns attempting
to ignore policy, reveal prompts or invoke tools set an injection signal and are
excluded from authority decisions. Tool schema validity is not authorization:
the executor still checks allowlists, budget, deadline and provider isolation.

Logs redact bearer credentials, email/phone-like values and configured sensitive
keys. Evidence artifacts contain case IDs and hashes rather than credentials.
