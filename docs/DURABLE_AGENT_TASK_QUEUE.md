# Durable Agent Task Queue

Crows stores agent tasks in SQLite. The optional queue worker can claim queued specialist tasks, execute them under the server-wide agent concurrency budget, and renew a bounded lease while work is active.

## Enable the worker

Set:

- `CROWS_AGENT_WORKER_ENABLED=true` to start the queue worker with the API lifespan (disabled by default)
- `CROWS_AGENT_WORKER_CONCURRENCY=3` to set the worker's local in-flight limit (default: the global agent limit, never higher than it)
- `CROWS_AGENT_WORKER_INTERVAL_SECONDS=1` to control the idle polling interval (bounded to 0.25–30 seconds)
- `CROWS_AGENT_GLOBAL_MAX_CONCURRENCY=3` to set the shared server-wide execution budget

Only tasks whose titles use a registered specialist prefix, such as `[research] Review this topic`, can execute through the specialist worker. The existing direct task API remains available.

## Lease and recovery semantics

- Claims are atomic and ordered oldest-first; a task can be leased by only one worker.
- Each claim increments `attempt_count`. A worker must present its owner ID and a live lease to execute or finalize the task.
- Workers renew 120-second leases every 30 seconds while waiting for execution capacity or running a specialist.
- A lease that expires is changed to `unknown`, not requeued. An interrupted task may already have performed work, so automatic replay could duplicate it. Inspect and explicitly reconcile uncertain work before creating a new task.
- Completion is fenced by the current lease owner and expiry. A stale worker cannot overwrite a newer or uncertain outcome.
- Shutdown cancels active worker executions and records an unknown outcome when the worker still owns a live lease.

The worker shares the existing server-process ownership lock. SQLite is the durable queue; this release does not claim multi-host or multi-process worker coordination.

## Queue health

`GET /tasks/queue/metrics` returns aggregate task counts and total claim attempts. It intentionally omits task IDs, worker IDs, and lease tokens.
