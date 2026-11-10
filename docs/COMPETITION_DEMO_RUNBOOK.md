# Crows — Competition Demo Runbook

This runbook is a repeatable, honest demonstration of the current MVP. It showcases local control, model routing, explicit review of coding changes, and optional isolated testing. It does not claim that the agent is fully autonomous or that cloud-model requests remain on the machine.

## Demo goal

Show one end-to-end flow:

1. Start the API on loopback and verify health.
2. Show configured model routes without exposing credentials.
3. Send a short chat request.
4. Select existing files in the configured workspace and generate a coding proposal.
5. Review the diff, then explicitly approve and apply it.
6. If Docker testing is enabled and the sandbox image exists, run tests and show the structured result.
7. Optionally demonstrate saved memory as an explicit, user-controlled feature.

**Safety boundary:** never expose the local API to a public network, show provider keys on screen, or approve a diff without reading it. The test runner operates on a disposable snapshot; it is not a guarantee that arbitrary code is safe.


## Workflow pause, resume, and cancellation

The selected workflow detail panel exposes **Pause**, **Resume**, and **Cancel workflow** controls for active runs. Control state is persisted separately from the workflow result status and remains available after a server restart.

- Pause is cooperative: the active bounded batch is allowed to finish, then no new specialist tasks are scheduled until resumed.
- Resume is accepted only from the paused state. A cancellation request cannot be reversed into running.
- Cancel is also cooperative: the active batch finishes, unscheduled tasks are marked as not executed, and the workflow finishes as `partial` without starting the QA task. The UI requires confirmation.
- These controls do not interrupt an external side effect already in progress. Review task/tool journals for uncertain outcomes; never assume cancellation rolled back an operation.
- Completed workflows cannot be reopened through these controls. Existing explicit recovery remains the only route for eligible interrupted (`unknown`) workflows.

## Reviewable agent planning

Use `POST /agents/plan` with a goal to generate a bounded, dependency-validated specialist plan. This endpoint is preview-only: it creates no workflow or task and returns `executed: false` plus `approval_required: true`.

Review the returned goal, task titles, inputs, roles, and dependency indexes. To execute exactly that reviewed plan, send `POST /agents/orchestrate` with both the original goal and the returned `subtasks` array. When `subtasks` is supplied, Crows validates and executes that exact plan without asking the planner model to regenerate it. Invalid or reserved roles are rejected before any workflow is created.

For compatibility, `POST /agents/orchestrate` without `subtasks` retains the existing generate-and-execute behavior. For a review-first workflow, always call `/agents/plan` first and explicitly submit the reviewed `subtasks`.

## Minimum AI-agent acceptance gates (must pass before submission)

The submission should prove that Crows executes agent work, not just that the API starts or a model produces fluent text. Run these checks on the exact release candidate and record the result honestly.

- [ ] **Reasoning and tool use:** ask a task that requires a workspace tool. Confirm the task journal shows the actual tool name, arguments, execution result, and final status. A guessed answer without a successful tool result is a failure.
- [ ] **Read a known file:** create a harmless file under the configured workspace with two unique lines. Ask chat to read that exact relative path and report both lines. Confirm `workspace.read_file` executed successfully in the task journal. Listing or searching the file is not a substitute for reading it.
- [ ] **Multi-step task:** ask for a small operation that requires more than one valid tool action, where supported. Confirm the answer is grounded in returned tool results; do not require multi-step behavior if the chosen provider does not support reliable tool calling.
- [ ] **Persistent state:** create a chat session, send a follow-up, and verify history persists. Separately save a harmless explicit memory, search it, and verify memory retrieval is opt-in.
- [ ] **Controlled coding:** create a proposal against a disposable sample project. Inspect the diff and verify the workspace remains unchanged before approval. If the Docker sandbox is enabled, show the real test result and verify the test-and-fix candidate remains pending until approval.
- [ ] **Failure honesty:** cause one safe, expected failure (for example, request a nonexistent file). Confirm the UI/log reports the actual error and the agent does not invent file contents.
- [ ] **Security and repeatability:** run the full test suite on the exact release commit; confirm localhost binding, API token protection, no visible secrets, and a clean working tree.

**Release gate:** do not claim a check passed based only on GitHub CI or this checklist. Mark each item only after observing the real local result. If file-reading/tool selection still fails, fix that before recording the demo. Freeze new features once these gates pass; architectural expansion belongs after submission.

## Before the demo

- [ ] Pull the latest `main` branch and run `python -m pytest -q`.
- [ ] Configure at least one OpenAI-compatible model route in the shell or through the UI's in-memory provider configuration panel.
- [ ] Configure `CROWS_API_TOKEN` and keep the value off-screen; enter it into the UI when prompted.
- [ ] Prepare the non-sensitive sample fixture from `examples/competition_demo/README.md` inside the configured `CROWS_WORKSPACE`; confirm it does not overwrite real project files.
- [ ] For the sandbox segment only, build the Docker image and set `CROWS_ENABLE_TEST_RUNNER=true`.
- [ ] Verify that the app binds to `127.0.0.1`, not `0.0.0.0`.
- [ ] Close unrelated windows and remove secrets or personal data from the sample workspace.

Provider variables are read from the process environment. This MVP does not load `.env` automatically. UI-configured provider credentials are memory-only and reset when the server restarts.

## Start and verify

From the repository root, activate the project's virtual environment and export the provider variables you need. Then start the service:

```bash
source .venv/bin/activate
python -m uvicorn crows.app:app --host 127.0.0.1 --port 8000
```

Open these local pages:

- UI: http://127.0.0.1:8000/ui/
- API docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health

Show `/health` and `/models` only as status information. `/models` reports route IDs/model IDs/configuration status, not API keys. If the token is enabled, enter it in the UI's token field.

## Agent self-introspection

The authenticated `GET /agent/status` endpoint and chat runtime context expose a live, secret-free snapshot of Crows' current mode, inferred task type, selected model route, configured routes, workspace path, registered tools and their schemas, skill activation state, recent task statuses, and coding test-runner status. Ask the assistant questions such as "Which model route is active?", "What tools can you use?", "Which skills are enabled?", or "What is your workspace?".

The snapshot intentionally excludes provider API keys, API tokens, passwords, task inputs, and task results. Recent task identifiers and status timestamps are included for operational transparency. Runtime introspection is informational: it does not grant additional tool permissions or bypass skill allowlists. If a runtime source is unavailable, Crows should report that field as unavailable rather than inventing it.

Verify locally with:

```bash
curl -sS http://127.0.0.1:8000/agent/status
```

When API-token protection is enabled, include the configured bearer token locally; never paste it into chat or include it in screenshots.

## Competition submission: maximum 3-minute demo

The competition demo is limited to 3 minutes. Record a concise, reproducible take that prioritizes one verified end-to-end agent task. Do not attempt to squeeze every feature into the submission video.

### Suggested 180-second structure

1. **0:00–0:20 — Problem and positioning.** Introduce Crows as a local-first, modular AI agent foundation for task execution with explicit review and safe local development. Avoid claiming production readiness or that all inference is local when a cloud provider is selected.
2. **0:20–0:40 — Live status.** Show the local UI and healthy API; briefly show the configured route without exposing provider IDs that are sensitive, tokens, or keys.
3. **0:40–1:05 — Real tool execution.** Ask a short task requiring a workspace tool. Show the task journal's actual tool name, arguments, result, and status. Only include this if the exact release candidate passes the tool-use acceptance gates above.
4. **1:05–2:25 — Approval-first coding.** Use a harmless sample project, create a proposal, inspect the diff, and show that files remain unchanged until explicit approval. Apply only after reviewing the diff. If this flow is not verified, do not stage it as a successful demo.
5. **2:25–2:50 — Verification and safety.** Show a real test result if the sandbox is available; briefly mention token protection and that coding changes require approval. Skip Docker claims if the sandbox was not actually run.
6. **2:50–3:00 — Close.** State the core value in one sentence and finish before the 3-minute cap.

**Recording rule:** use only features that passed on the exact release candidate. If live provider latency is unreliable, record a real successful run in advance; do not replace it with a mock or imply that an unexecuted action succeeded. Keep API tokens and provider credentials off-screen.

## Submission package checklist

Before submitting, verify the competition portal's current field requirements and prepare these items:

- [ ] **Project name:** Crows.
- [ ] **Project overview:** concise description of the problem, target users, and value.
- [ ] **Scenario:** one concrete user workflow demonstrated in the video.
- [ ] **Technical approach:** local-first API/UI, configurable model routes, workspace tools, persistent task/session state, approval-gated coding, and optional Docker test isolation—only describe capabilities present in the submitted revision.
- [ ] **Demo video:** no longer than 3 minutes; use the verified outline above.
- [ ] **Documentation:** repository README and this runbook reflect the actual submitted version.
- [ ] **Final evidence:** record the exact main commit, local test result, which acceptance gates passed, and any feature omitted from the demo.
- [ ] **Security review:** remove secrets and personal data from the recording, screenshots, sample workspace, and submission text.

Do not mark the submission complete until each required portal field is filled and the video duration is checked.

## Fallback plan

- **Provider unavailable:** demonstrate health, route configuration, workspace listing, and the documented approval-first flow. Clearly state that live model calls are unavailable; do not present a mock response as a real model response.
- **Docker unavailable:** skip the sandbox segment and explain that Docker is optional for the core API but required for the Docker-backed test runner.
- **No sample project:** prepare one in advance. The coding workflow targets explicitly selected existing files and does not support arbitrary shell execution or creating new files through the proposal workflow.
- **Unexpected failure:** capture the exact error, stop the affected segment, and continue only with features that still work. Do not silently claim success.

## Claims to make — and avoid

**Supported by the current MVP**
- Local FastAPI service and SQLite-backed task/session/memory persistence.
- Configurable OpenAI-compatible provider routes with bounded retries and transient-error fallback.
- Explicit review and approval before coding proposals are applied.
- Optional Docker-backed pytest on a disposable snapshot.
- API route discovery that does not return provider credentials.

**Do not claim**
- Fully autonomous or production-ready operation.
- Benchmark-driven automatic model selection or dynamic provider discovery.
- Perfect sandbox isolation or guaranteed safety for hostile code.
- That every inference stays local when a cloud provider is configured.
- That passing tests proves a change is correct in every situation.

## Final checklist

- [ ] Latest test suite passed locally.
- [ ] API health is OK and intended provider route is configured.
- [ ] Demo workspace contains only safe, non-sensitive sample files.
- [ ] API remains bound to loopback.
- [ ] Token and provider keys are not visible in screen recording.
- [ ] Every proposed diff is reviewed before approval.
- [ ] Any skipped or failed segment is described honestly.
