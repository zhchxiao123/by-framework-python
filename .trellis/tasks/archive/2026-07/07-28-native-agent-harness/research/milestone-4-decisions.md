# Milestone 4 Redis Durable Runtime Decisions

- Run transition commits retain the Milestone 0 single-Lua expected-version and
  fencing contract. `RedisRunStore.load` recovers the materialized state and
  ordered event stream atomically after coordinator takeover. Commits also read
  the current coordinator fence from the run-local lease hash, rejecting an old
  owner immediately after takeover even before the new owner commits.
- Coordinator and step leases share one run-tagged Redis hash. Coordinator
  takeover increments a monotonic fence; each step retry increments its own
  lease token, while duplicate lease delivery for the same attempt returns the
  existing token. Result submission validates run version, attempt,
  coordinator fence, step token, and step expiry. Identical duplicate results
  are idempotent and conflicting duplicates are rejected.
- Submitted step results remain in the lease hash so a replacement coordinator
  can recover a result written before the prior coordinator crashed and before
  the state transition committed.
- Checkpoints are immutable versioned values plus a sorted index. Retrying a
  write after a crash between immutable `SET` and index update repairs the
  index. Definitions are content-addressed immutable values.
- Distributed step dispatch uses the existing `AskAgentCommand` and agent-type
  control stream with legacy callback routing disabled. Native step metadata is
  additive `extra_payload`; no second Worker consume loop or lifecycle was
  introduced.
- Remote Agent completion uses durable correlation fields with `HSETNX` result
  resolution, making duplicate resume delivery idempotent.

## Validation Boundary

- Integration tests use a deterministic behavioral Redis double, including Lua
  contract emulation. They cover duplicate delivery and selected crash windows
  but are not a production Redis Cluster or multi-process certification.
- Production lease scripts use Redis `TIME`, eliminating cross-host client clock
  skew. A client clock override exists only for deterministic contract tests.
- The generic Step Worker that executes native plan nodes and Redis-backed
  subscription/wakeup services remain deployment work. The current transport
  deliberately maps onto existing Worker routing without changing
  `WorkerRunner`.
- Remote-result correlation is durable and idempotent, but automatically wiring
  it into every graph interrupt across process restart requires the definition
  catalog/server lifecycle planned for the deployment milestone.
