# Milestone 0 Spike Decisions

## Controlled serialization

- Persisted definitions use canonical UTF-8 JSON with sorted object keys,
  compact separators, and finite JSON numbers.
- The accepted domain is explicit: JSON primitives, string-keyed mappings,
  sequences, enums through their values, and dataclass fields.
- Bytes, sets, callables, non-string mapping keys, non-finite floats, and
  arbitrary object fallback are rejected. There is no pickle fallback.
- Plan identity is `sha256:<hex>` over the canonical bytes. Definition schema
  versions remain fields in the hashed document rather than hash configuration.

## Coordinator fencing

- Coordinator ownership has a monotonically increasing fencing token. Renewal by
  the same owner while its lease is live keeps its token; any acquisition after
  expiry increments it, including reacquisition by the same owner identity.
- Every step lease binds the run version, coordinator fence, attempt, and a
  per-step lease token. Retrying a step supersedes its prior token.
- Only the coordinator may validate and commit results. A result from an old
  coordinator, prior step lease, or state version is rejected before reduction.

## Atomic run commit

- The store boundary exposes one expected-version commit containing transition
  events and the next materialized state.
- A commit also carries the coordinator fence. Redis rejects both version
  conflicts and fences lower than the latest committed fence.
- Redis state and event-stream keys contain the same `{run_id}` hash tag. A
  single Lua invocation performs the version/fence check, appends all events,
  and advances state, so it is atomic and valid in Redis Cluster.
- The event log remains authoritative; state in this spike is the materialized
  projection advanced in the same transaction.

## Deferred after the spike

- Public `Agent`, graph, event taxonomy, and checkpoint APIs.
- Redis-backed coordinator lease acquisition/renewal and process crash tests.
- Production retention/checkpoint policy and schema migration.
- Provider streaming normalization, which is independent of the three required
  Milestone 0 exit contracts and belongs with the provider contract slice.
