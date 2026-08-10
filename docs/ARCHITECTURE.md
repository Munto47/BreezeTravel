# BreezeTravel architecture

## Request-to-itinerary path

```text
HTTP identity/member check
  -> Task Parser -> TripTaskSpec
  -> bounded Router -> deadline-aware Tool Runtime
  -> Planner subgraph
  -> three-state Verifier
  -> targeted Repair (at most 2 rounds)
  -> VerificationReport + itinerary snapshot hash
  -> SSE/UI constraint panel
```

`TripTaskSpec` is the planning contract. It separates explicit hard constraints,
soft preferences, collaboration policies, evidence requirements and stable memory.
Generic policies such as “保留多数投票地点” are represented as policies rather
than being misread as POI names.

The Planner keeps the existing cluster, distance, sequence, schedule and tips
nodes, then runs deterministic rules. A check is `SATISFIED`, `VIOLATED` or
`UNKNOWN`; missing weather, travel-time or complete vote evidence is never
reported as satisfied. Repair is rule-targeted, capped at two rounds and followed
by a fresh verification report.

## State ownership

- PostgreSQL owns rooms, members, memories, task specifications, verification
  reports and LangGraph checkpoints.
- Redis owns shared rate-limit and transient coordination state.
- Yjs owns live collaborative document state and persists updates to its own
  mounted data directory. JWT room tokens bind a user to one room.
- The UI treats a verification report as valid only while its itinerary snapshot
  hash matches the currently rendered itinerary.

Database mutation is performed by `python -m scripts.migrate`; application
startup checks schema compatibility and opens the checkpointer, but does not run
DDL. Non-demo startup fails closed when checkpoint persistence is unavailable.

## Tool and RAG boundary

Every tool call has an envelope, budget, deadline, concurrency limit and receipt.
Retries are limited to retryable errors; circuit state is isolated per provider.
Retrieved documents are untrusted data. Injection-like text is signalled and is
not allowed to change tool permissions or system instructions.

RAG routing is dynamic: exact POI and short factual queries can skip HyDE;
hotel/food/tips queries can enable Multi-Query; the policy records its decision in
evaluation evidence. The controlled local ablation uses an in-memory retrieval
proxy and is not presented as production pgvector performance.
