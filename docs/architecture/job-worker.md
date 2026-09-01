# Benchmark Job and Worker

## Responsibility boundary

The API creates and manages durable jobs; it does not execute benchmark code. One or more independent `benchmark-worker` processes consume the queue and invoke the configured Local or Docker backend. Private oracles remain server-side and are never returned by job APIs.

## State machine

```text
queued -> preparing -> evaluating -> completed
   ^          |             |
   |          +-------------+-> queued (retry available)
   |                        +-> failed (attempt limit reached)
   +---------------- retry <- failed/cancelled

queued/running -- cancel request --> cancelled
```

Each claim increments `attempt` and creates an immutable-identity `JobAttempt`. A `BEGIN IMMEDIATE` SQLite transaction serializes selection and assignment so concurrent workers cannot claim the same job.

## Lease and recovery

Workers refresh `heartbeat_at` and `lease_expires_at` during execution. Before claiming new work, a worker recovers expired `preparing` or `evaluating` jobs. The interrupted attempt is marked `expired`; the job is requeued when attempts remain, otherwise it becomes `failed`.

This provides at-least-once execution. Result consumers must use job/result identifiers rather than assuming the backend runs exactly once.

## Cancellation and retries

Queued jobs cancel immediately. Running jobs set `cancellation_requested`; workers observe it at backend boundaries, so cancellation latency is bounded by the active backend command timeout. Automatic failures use exponential delay capped at 60 seconds. An explicit retry is allowed only for terminal `failed` or `cancelled` jobs and extends the attempt allowance when necessary.

## Operations

```powershell
sdd-eval benchmark-worker --db sdd_eval.db --concurrency 4
sdd-eval benchmark-worker --db sdd_eval.db --once
```

Run workers under a service manager and stop them gracefully. SQLite is appropriate for one-host execution; multi-host scheduling should replace the claim layer with a transactional shared queue while preserving the job contract.
