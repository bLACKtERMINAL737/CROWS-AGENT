# Crows

A local-first, modular AI agent foundation focused on reliable task execution, explicit approval, and safe-by-default local development.

## Features

- **Local FastAPI service** with OpenAPI documentation and a lightweight review UI.
- **Local API protection** with an optional shared bearer-style token and same-origin checks for state-changing requests.
- **Persistent task journal** backed by SQLite, including explicit tool-execution states and crash-safe handling of interrupted work.
- **Durable agent workflows** that persist the validated plan, specialist task IDs/results, and final QA checkpoint, with read endpoints for later inspection.
- **Reviewable agent plans**: preview a validated specialist dependency graph without creating tasks, then explicitly execute the exact reviewed plan.
- **Durable workflow controls**: pause between active task batches, resume paused workflows, or request cancellation without replaying uncertain work; control state survives server restarts.
- **Task Journal UI panel** to create local task records, review statuses, and inspect recorded tool-execution details using the existing task API. Creating a record does not automatically execute the task.
- **Workspace tools** for listing files and searching text while preventing paths from escaping the configured workspace.
- **Auto and Manual model modes**: Auto locally classifies task intent and selects a configured route; Manual lets the user explicitly select a configured route. Neither mode exposes credentials.
- **Model route discovery** via `GET /models`, returning only route IDs, model IDs, and configured status.
- **Task-specific model routing** for primary, coding specialist, optional reasoning specialist, and fallback providers; each route can use a different OpenAI-compatible endpoint/model.
- **Bounded provider retries** for transient network, timeout, and HTTP 5xx failures, with a configurable total attempt budget (1–6) across routes and route-labelled failure logs.
- **Configurable model request timeout** shared across primary, coding, reasoning, and fallback routes, with validated 1–600 second bounds.
- **Coding proposals** that show file diffs and require explicit approval before changes are applied.
- **Approval-gated new-file proposals** that can create explicitly declared workspace files, without overwriting existing paths and with rollback protection if a multi-file apply fails.
- **Proposal state protection** so only valid pending proposals can be approved or rejected, and already-applied changes cannot be applied again.
- **Test-and-fix workflow** with up to three rounds on a disposable workspace copy; only a passing candidate becomes a proposal.
- **Optional Docker test sandbox** for running pytest against a disposable snapshot.
- **Persistent chat sessions** with message history and optional, per-request saved-memory retrieval.
- **Explicit local memory** with category/tag metadata, keyword search, listing, and deletion.
- **Automated tests** and a GitHub Actions test workflow.

## Safety and scope

Crows is an evolving MVP, not a production-ready autonomous agent.

- Keep the API bound to localhost. Do not expose the service or UI to a public network.
- Set `CROWS_API_TOKEN` to require a shared token for API routes. Health, UI assets, and API schema/docs remain reachable without that token.
- If no token is configured, API requests are restricted to loopback clients. This is a development fallback, not protection from other local processes.
- The UI keeps the entered token in the current browser tab's session storage and sends it using the `X-Crows-Token` header.
- State-changing requests that include an `Origin` header must use the same loopback host and port. This is defense in depth, not a replacement for keeping the service local.
- Coding proposals can modify explicitly selected existing files or create explicitly declared new workspace files, but only after explicit approval. Arbitrary shell access is not provided by the coding proposal workflow.
- Proposal changes require review and explicit approval. Applying a proposal checks the original file state to detect stale files.
- The Docker test runner is disabled by default. When enabled, it uses a prebuilt image, disables networking, drops Linux capabilities, runs as a non-root user, sets resource limits, uses a read-only container root, and tests a disposable workspace snapshot.
- Docker is an additional isolation layer, not a perfect security boundary. Keep Docker and the host updated, and do not run hostile workloads on a sensitive machine.
- Native tool calls are rejected if their names are not registered for the request; malformed or non-object JSON arguments are returned as a safe tool error without invoking the tool callback. Tool execution exception details are redacted before being sent back to the model.
- Saved-memory retrieval is opt-in for each chat request. Retrieved records are passed as untrusted reference data, not instructions. Do not store secrets in chat or memory.
- Auto routing uses a local task-intent heuristic and configured task specialists; a reasoning specialist is optional. Automatic benchmark-driven cost/latency optimization and a dynamic provider catalog are not implemented yet.
- Do not commit API keys, access tokens, or other secrets.

## Requirements

- Python 3.11 or newer
- Docker is optional, but required for the Docker-backed test runner

## Installation

From the repository root:

```bash
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
# .venv\\Scripts\\Activate.ps1

pip install -e ".[dev]"
cp .env.example .env
```

Configuration is read from the process environment. This MVP does **not** load `.env` automatically; export values with your preferred local environment manager.

### Configure API protection

Generate a local token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
export CROWS_API_TOKEN='paste-generated-token-here'
```

On PowerShell:

```powershell
$env:CROWS_API_TOKEN="paste-generated-token-here"
```

Start the server after setting the variable, then enter the same token in the local UI. Never commit the token.

### Configure model providers

Set these environment variables for the routes you want to use:

| Variable | Purpose |
| --- | --- |
| `CROWS_MODEL_BASE_URL` | Base URL for the default OpenAI-compatible API |
| `CROWS_MODEL_API_KEY` | API key for the default route |
| `CROWS_MODEL_NAME` | Model identifier for the default route |
| `CROWS_CODING_MODEL_BASE_URL` | Optional coding-specialist API base URL |
| `CROWS_CODING_MODEL_API_KEY` | Optional coding-specialist API key |
| `CROWS_CODING_MODEL_NAME` | Optional coding-specialist model identifier |
| `CROWS_FALLBACK_MODEL_BASE_URL` | Optional fallback API base URL |
| `CROWS_FALLBACK_MODEL_API_KEY` | Optional fallback API key |
| `CROWS_FALLBACK_MODEL_NAME` | Optional fallback model identifier |
| `CROWS_REASONING_MODEL_BASE_URL` | Optional reasoning-specialist API base URL |
| `CROWS_REASONING_MODEL_API_KEY` | Optional reasoning-specialist API key |
| `CROWS_REASONING_MODEL_NAME` | Optional reasoning-specialist model identifier |
| `CROWS_DB_PATH` | SQLite database path; default `./data/aether.sqlite3` (preserved for existing installations) |
| `CROWS_MESSAGING_TARGET_KEY` | Optional Fernet key used to encrypt persisted outbound delivery destinations; required before a background worker can resume Telegram deliveries |
| `CROWS_WORKSPACE` | Workspace directory allowed for workspace tools; default `./workspace` |
| `CROWS_ENABLE_TEST_RUNNER` | Set to `true` to enable Docker-backed pytest; default `false` |
| `CROWS_TEST_TIMEOUT_SECONDS` | Test timeout in seconds, clamped to 5–120 |
| `CROWS_SANDBOX_IMAGE` | Local sandbox image name; default `crows-sandbox:latest` |
| `CROWS_TEST_PROJECT` | Optional project path to test instead of the configured workspace |

Generate a messaging-target encryption key (keep it private and back it up securely):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export CROWS_MESSAGING_TARGET_KEY='paste-generated-key-here'
```

The key is never stored in SQLite. Losing it makes previously encrypted delivery destinations unreadable. Outbound message content and delivery state remain in the inbox/outbox; the destination is stored separately as authenticated ciphertext. Discord stores only the fixed `configured-webhook` reference, never the webhook URL. Without this key, current synchronous sends continue to work, but no recoverable Telegram target is persisted and a future background worker must not claim those messages.

If model-provider variables are absent, local health checks and tools can still work, but chat/coding requests that need a model will return a configuration error.

## Build the optional Docker sandbox

Build the image once from the repository root. At runtime, Crows uses `--pull=never`, so it will not pull images automatically.

```bash
docker build -f Dockerfile.sandbox -t crows-sandbox:latest .
export CROWS_ENABLE_TEST_RUNNER=true
export CROWS_SANDBOX_IMAGE=crows-sandbox:latest
```

PowerShell:

```powershell
$env:CROWS_ENABLE_TEST_RUNNER="true"
$env:CROWS_SANDBOX_IMAGE="crows-sandbox:latest"
```

The test runner works on a temporary copy. Its Docker configuration disables network access, uses a read-only root filesystem, drops capabilities, enables `no-new-privileges`, runs as a non-root UID, and applies PID, memory, CPU, and timeout limits. Test changes to the snapshot are discarded.

## Run the API and UI

```bash
uvicorn crows.app:app --host 127.0.0.1 --port 8000
```

Open these local URLs:

- UI: http://127.0.0.1:8000/ui/
- API documentation: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

## Agent workflows

The agent endpoints have different execution guarantees:

- `POST /agents/plan` asks the configured model for a bounded plan and does not execute it.
- `POST /agents/delegate` validates a supplied plan and creates queued task records; it does not run them.
- `POST /agents/plan` generates a bounded, validated specialist plan for review without creating a workflow or tasks. It returns `executed: false` and `approval_required: true`.
- `POST /agents/orchestrate` validates a model-generated plan and dependency graph, stores a durable workflow record, runs up to three dependency-ready specialist tasks concurrently, blocks downstream tasks when prerequisites fail, and checkpoints each task result plus a final QA/security review. To execute a reviewed plan exactly as shown, submit its `subtasks` array with the original `goal`; Crows validates and executes that supplied plan without regenerating it. For backward compatibility, omitting `subtasks` retains generate-and-execute behavior. Each specialist and QA task has a configurable timeout (`CROWS_AGENT_TASK_TIMEOUT_SECONDS`, default 120 seconds, clamped to 1–600); timed-out tasks are marked `unknown` and their dependents are blocked rather than replayed. The concurrency limit is configurable with `CROWS_AGENT_MAX_CONCURRENCY` (default 3, clamped to 1–3). Workflow checkpoints also expose structured diagnostics: status counts and a bounded failure list that distinguishes timeouts, uncertain outcomes, task failures, dependency blocks, and QA failures. If an orchestration request is cancelled, active task outcomes are conservatively marked `unknown`, the workflow checkpoint records the interruption, and automatic replay is avoided. Clients may include an optional `idempotency_key` (1–256 characters) in the request body and must reuse it for retries of the same logical request. A matching key and request returns the original saved workflow without replanning or re-executing it; reusing a key with a different goal or supplied plan returns HTTP 409. Key creation is atomic across concurrent requests. Requests without a key retain the legacy behavior.
- `GET /agents/workflows?limit=50` lists recent workflow runs.
- `GET /agents/workflows/{workflow_id}` returns the saved plan, task checkpoints, QA result, timestamps, and workflow status.
- `GET /agents/workflows/{workflow_id}/timeline` returns ordered workflow/task/tool status events and timestamps. It omits goals, task titles/inputs, tool arguments, raw results, errors, and internal task/tool IDs.
- `POST /agents/workflows/{workflow_id}/recover` explicitly continues an interrupted workflow only when its recorded queued tasks are still queued in the task journal.

A completed workflow is marked `succeeded` only when every specialist task and the final QA task succeeds; otherwise it is marked `partial`. An interrupted running workflow is marked `unknown` at startup and its checkpoints are retained for inspection. Recovery is always explicit: call `POST /agents/workflows/{workflow_id}/recover` only after reviewing the saved record. It executes only tasks still marked queued in both the workflow checkpoint and task journal and whose prerequisites succeeded; running, failed, succeeded, and unknown tasks are never reset or replayed. Recovery is atomically claimed so concurrent requests cannot both resume the same workflow. If the QA task ID was not checkpointed before interruption, recovery does not create a replacement automatically; it marks QA as not started and recommends a fresh workflow. Orchestration uses the existing read-only workspace/public-web tools; it does not modify project files. Code changes still require the separate approval-gated coding workflow.

## Coding workflow

1. Place the project you want to work on inside the configured workspace.
2. Open the local UI and select existing files.
3. Describe the change and generate a proposal (or use the iterative test-and-fix workflow).
4. Review the proposed diff.
5. Approve the proposal explicitly.
6. Apply the approved proposal.
7. If configured, run tests in the disposable Docker sandbox.

Relevant API endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/coding/proposals` | Generate a coding proposal |
| `POST` | `/coding/iterate` | Run the bounded test-and-fix workflow |
| `GET` | `/coding/proposals/{proposal_id}` | Inspect a proposal |
| `POST` | `/coding/proposals/{proposal_id}/approve` | Approve a pending proposal |
| `POST` | `/coding/proposals/{proposal_id}/reject` | Reject a pending proposal |
| `POST` | `/coding/proposals/{proposal_id}/apply` | Apply an approved proposal |
| `GET` | `/coding/files?path=.` | List workspace entries |
| `POST` | `/coding/run-tests` | Run tests if the sandbox is enabled |

The iterative endpoint accepts `instruction`, `paths`, and optional `max_rounds` (1–3).

## Chat sessions and saved memory

Chat sessions and messages are persisted in SQLite. Follow-up requests can include up to 12 recent user/assistant messages. The UI supports creating, reopening, and deleting conversations.

Saved-memory retrieval is **off by default for every request**. Enable it explicitly when sending a chat message to retrieve up to five relevant records. Retrieval uses local lexical keyword matching, not embeddings or semantic search.

Session endpoints:

- `POST /sessions` — create a session
- `GET /sessions` — list sessions
- `GET /sessions/{session_id}` — retrieve a session and its messages
- `GET /sessions/export?limit=100` — export up to 1,000 recent sessions with up to 10,000 messages per session in a versioned JSON archive (`crows-conversation-export`, version 1). Treat exported files as private conversation data.
- `DELETE /sessions/{session_id}` — delete a session and its messages
- `POST /chat` — chat with optional `session_id`, `task_type`, and `use_saved_memories` (default `false`)

Memory endpoints:

- `POST /memory` — save a memory with a category, content, optional tags, and source
- `PATCH /memory/{memory_id}` — update a saved memory's category, content, and/or tags; omitted fields remain unchanged
- `GET /memory?limit=100&category=preference` — list memories
- `POST /memory/search` — search saved records by keywords
- `DELETE /memory/{memory_id}` — delete a saved record
- `GET /memory/export` — export saved records as a versioned JSON document (`format: crows-memory-export`, `version: 1`) for local backup or migration. The export contains memory content and metadata; keep the downloaded file private.

Memory is explicit and local; the application does not silently infer or save memories from chats.

## Reliability notes

- SQLite WAL and transactions are used for local persistence.
- Interrupted in-flight tasks, tool executions, and running agent workflows are marked `unknown` rather than replayed automatically.
- Agent workflow plans, dependency graphs, task outcomes, and QA checkpoints are persisted in SQLite and exposed through read-only workflow endpoints. Recovery validates dependency graphs, task identity/status, plan indexes, QA checkpoint structure, and agreement between each checkpoint outcome and its durable task journal before any task can run. Workflow status updates are guarded against terminal-state regression; `unknown` workflows must be atomically claimed before recovery can return them to `running`. Task journal updates reject stale writes that would reopen succeeded, failed, or unknown tasks, and prevent running tasks from regressing to queued; same-state writes remain available for journal enrichment; omitted fields preserve prior values, and terminal task outcomes cannot be replaced by later same-state payloads. Tool execution journals likewise accept finalization only from `running` (or same-state enrichment), preventing late results from overwriting `succeeded`, `failed`, or `unknown` outcomes; omitted fields are preserved and terminal tool results/errors remain immutable. Terminal workflow checkpoints preserve their first final result/error payload, while running workflow checkpoints can continue to advance. Fresh orchestration runs at most three independent tasks concurrently; dependent work waits for all prerequisites to succeed. Explicit recovery remains sequential in validated topological order and never replays running, failed, succeeded, or unknown tasks.
- Coding proposal application verifies original file hashes before replacing files.
- Proposal state transitions are checked to prevent invalid approval/rejection and duplicate application.
- Sandbox tests run without network access and do not pull images at runtime.

### Model routing, retries, and fallback

Crows selects a provider using the requested task type:

- `coding` uses the configured coding-specialist provider when available.
- Other tasks, and coding tasks without a configured coding provider, use the primary provider.
- The optional fallback provider is tried only after the selected provider fails with a transient error. It is not a general-purpose second choice when a request is rejected.

Transient failures eligible for retry/fallback are network errors, timeouts, and HTTP 5xx responses. HTTP 4xx responses (for example, authentication or invalid-request errors) and other non-transient errors are surfaced without trying the fallback route.

The chat UI supports **Auto** and **Manual** model modes. Auto mode uses a local, deterministic task-intent heuristic (no extra model call) and routes coding tasks to the configured coding specialist; other task classes currently use the primary route unless explicitly configured otherwise. Manual mode requires an explicit configured route (`primary`, `coding`, or `fallback`). Fallback can be disabled per chat request; when enabled, fallback is used only for transient errors. `GET /models` lists configured routes without returning API credentials. This initial routing heuristic is intentionally conservative and should be evaluated against real tasks before claiming benchmark-driven optimization.

The router allows **1–3 attempts per provider** (`max_attempts`; default **2**). Retries of the same provider use a short increasing delay: 0.25 seconds before the second attempt, then 0.5 seconds before the third. If the selected provider exhausts its attempts with a transient error, the configured fallback gets its own bounded attempts. If fallback also fails, Crows raises the last provider error.

Successful chat responses include `route`, `model`, `fallback_used`, `attempts`, and a sanitized `attempt_history` list. Each history entry records the route, total attempt number, outcome, and (for failures) exception type only; it deliberately excludes exception messages, prompts, response bodies, and credentials. The attempt count includes calls made to both the selected and fallback providers. If no usable route is configured, the router raises a configuration error.

**Important:** a timeout or connection failure does not always prove that the remote provider stopped processing the request. A retry may therefore cause duplicate processing or billing, depending on the provider. Retries are bounded to limit this risk.

## Tests

Run the suite locally:

```bash
python -m pytest -q
```

The last reported local verification on `main` before the durable-workflow checkpoint changes was **405 passed**. Run the suite on your current branch before merging; GitHub Actions is also configured.

## Current development priorities

- Expand security tests around concurrent approval/apply requests and crash scenarios.
- Improve provider error classification and reliability measurements.
- Benchmark latency, reliability, and cost before introducing automatic cost-based routing.
- Continue hardening the local review and coding workflow.


### Durable messaging outbox worker

The outbox worker is opt-in and disabled by default. Configure `CROWS_MESSAGING_TARGET_KEY` with a persistent Fernet key before enabling it. Then set:

```bash
export CROWS_MESSAGING_WORKER_ENABLED=true
export CROWS_MESSAGING_WORKER_INTERVAL_SECONDS=2
```

The worker sends only target-backed messages that it can atomically lease. Telegram destinations are decrypted in the backend; Discord resolves either the fixed `configured-webhook` reference or a signed interaction token stored in encrypted form. Transport errors and expired leases become `unknown` and are not automatically retried because delivery may already have occurred. The worker stops with the FastAPI application. Run one application worker process against a SQLite database; multi-process deployment needs a separately evaluated worker topology.

Discord also supports signed `/crows message:<text>` interactions through `POST /messaging/webhooks/discord`. Configure `CROWS_DISCORD_APPLICATION_ID`, `CROWS_DISCORD_PUBLIC_KEY`, the persistent `CROWS_MESSAGING_TARGET_KEY`, and both messaging workers. See [Discord interactions](docs/DISCORD_INTERACTIONS.md). This is slash-command intake, not a Discord Gateway listener.


### Messaging outbox retry and reconciliation

The opt-in outbox worker retries only explicit HTTP 429 rate-limit responses, honoring a bounded `Retry-After` delay (1–300 seconds) and at most three scheduled retries. Other HTTP failures and transport errors are not automatically retried; ambiguous outcomes remain `unknown`. Exhausted rate-limited deliveries become `failed`.

After checking the provider directly, an operator can resolve an `unknown` outbound record through the authenticated `POST /messaging/outbox/{message_id}/reconcile` endpoint with a final status (`sent`, `delivered`, or `failed`), a short evidence note, and optionally the provider message ID. Reconciliation is one-way and recorded in an audit table; it does not resend a message.


### Durable execution health metrics

The authenticated `GET /agents/execution/metrics` endpoint reports aggregate counts for queued, running, succeeded, failed, and uncertain tasks; workflow states; and tool execution states. It also reports the oldest queued-task and unknown-workflow timestamps. It deliberately omits task IDs, goals, inputs, results, tool arguments, and error text.

### Deterministic workflow evaluation

The authenticated `GET /agents/workflows/{workflow_id}/evaluation` endpoint evaluates a saved workflow without making model calls. It checks plan/checkpoint consistency, specialist task outcomes, final QA verdict, and aggregate tool-call counts. A workflow is marked `passed` only when the workflow itself succeeded, every planned task has a successful checkpoint, and the final QA verdict is an explicit `PASS`. Incomplete, failed, malformed, or inconsistent checkpoints are reported conservatively. The report omits workflow goals, task prompts, task IDs, tool arguments, and raw task results. The authenticated `GET /agents/evaluations?limit=50` endpoint provides content-free aggregate outcomes, pass rate, task-status counts, QA verdict counts, and tool-call totals across recent workflows; it never returns workflow IDs or task content.


### Declarative skills registry (review-gated foundation)

Crows includes a local registry for versioned skill manifests. A manifest declares a short description, an allowlist of already-registered tools, and the read permissions required by those tools. Skill versions are immutable; reusing a version with different content is rejected. New versions remain disabled until an authenticated user explicitly approves activation. An active version can be disabled or rolled back by selecting an earlier immutable version.

The registry stores metadata under the configured database directory's `skills/` folder with restrictive local permissions and atomic activation-state writes. It does **not** execute Python, shell commands, downloaded code, or arbitrary prompt files from a skill package. To use approved skills in a chat request, pass their IDs in the optional `skill_ids` array (one to five IDs). For example, `POST /chat` accepts `{"message":"Review this repository","skill_ids":["repo-review"]}`. Every selected skill must currently be active. The model receives only the union of tools explicitly allowed by those active manifests, and the server rejects tool calls outside that set and rechecks activation before each execution. Omitting `skill_ids` preserves the normal unrestricted-by-skill chat tool catalogue. Skills are explicitly selected by the caller; automatic model-driven skill discovery, authoring, and self-improvement are not implemented.
