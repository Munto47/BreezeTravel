# Reliability model

## Runtime controls

- Request-level deadline is propagated through chat and tool execution.
- Client disconnect cancels the SSE pipeline; cancellation is not retried.
- Tool calls have per-provider semaphores, bounded retry with backoff, circuit
  breakers and machine-readable receipts.
- Unknown tools and invalid payloads fail as typed `INVALID_PAYLOAD` results.
- Rejected calls still consume/record tool budget so the ReAct loop cannot hide
  unbounded attempts.
- Rate limiting uses one Redis atomic sliding window shared by instances.

Prometheus metrics cover request latency/status, Agent success, critic and repair
rates, ReAct iterations, tool distribution/errors, circuit state and verifier
state. Structured logs carry trace, room, thread and instance identifiers while
the redaction layer removes authorization tokens and common PII fields.

## Controlled-local failure evidence

`python -m scripts.run_fault_injection` executes 24 fixed profiles across timeout,
retry, circuit-open, malformed payload, missing evidence, RAG degradation and
Yjs persistence boundaries. The current report passes 24/24. No result is inferred
from configuration alone.

`python -m scripts.validate_multi_instance` calls both backend instances with one
thread. Current evidence records checkpoint counts growing from 5 to 10 after the
second instance, and a concurrent Redis limit allowing exactly 3 of 6 attempts.

Pinned nginx/Prometheus images could not be downloaded during the current run
because the configured Docker mirror timed out during TLS negotiation. Therefore
the two instances were validated through separate localhost ports, and the
report does not claim an nginx load-balancer or Prometheus-container runtime
test. Their configs do pass static Compose/YAML validation.
