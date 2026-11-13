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

- Claims are atomic and ordered by effective priority, then oldest-first; a task can be leased by only one worker. Base priority ranges from -10 to 10. Priority aging adds one effective-priority point per 30 minutes queued, so long-waiting work eventually outranks newer work without changing its stored base priority. The aging interval is currently fixed at 30 minutes.
- Each claim increments `attempt_count`. A worker must present its owner ID and a live lease to execute or finalize the task.
- Task creation accepts an optional `priority` from -10 to 10 and an optional `idempotency_key`; repeating the same key and payload returns the original task, while reusing the key for a different title, input, or priority returns HTTP 409.
- `POST /tasks/{task_id}/retry` explicitly requeues only known `failed` tasks, with a bounded `max_retries` (default 3, maximum 10). `unknown` tasks cannot be retried automatically or through this endpoint.
- Workers renew 120-second leases every 30 seconds while waiting for execution capacity or running a specialist.
- A lease that expires is changed to `unknown`, not requeued. An interrupted task may already have performed work, so automatic replay could duplicate it. Inspect and explicitly reconcile uncertain work before creating a new task.
- Completion is fenced by the current lease owner and expiry. A stale worker cannot overwrite a newer or uncertain outcome.
- Shutdown cancels active worker executions and records an unknown outcome when the worker still owns a live lease.

The worker shares the existing server-process ownership lock. SQLite is the durable queue; this release does not claim multi-host or multi-process worker coordination.

## Queue health

`GET /tasks/queue/metrics` returns aggregate task counts, attempts, queued counts by base priority, oldest queued timestamp and age in seconds, plus the highest effective priority under the 30-minute aging policy. It intentionally omits task IDs, worker IDs, and lease tokens.


## Scheduler recovery controls

- A schedule can be explicitly retried only after it reaches the definitive `failed` state: `POST /scheduler/schedules/{id}/retry` with a timezone-aware `run_at` and an operator reason.
- Each retry increments the durable `retry_count` and records the previous task ID, reason, requested time, retry number, and timestamp in the schedule retry audit.
- `GET /scheduler/schedules/{id}/retry-audit` exposes that history for operator review.
- Running, scheduled, completed, cancelled, and especially `unknown` schedules cannot use this retry path. Unknown execution must be reconciled against the task journal/provider evidence first; it is never blindly replayed.
