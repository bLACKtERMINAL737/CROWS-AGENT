# Crows — Master Engineering Roadmap

Planning baseline: repository source/docs inspected on 29 October 2026.
Competition deadline: organizer details supplied by the project owner list 10 November 2026 for preliminary entries. Re-check the portal before submitting.

## Product thesis
Crows is a local-first, model-flexible agent platform for controlled, verifiable execution of developer and knowledge-work tasks. Users should be able to choose automatic or manual model routing, inspect plans and tool actions, recover from interruptions without unsafe replay, and approve consequential changes.

“More capable” must be demonstrated with reproducible end-to-end tasks, transparent outcomes, security tests, latency/cost measurements, and a useful demo. Do not claim superiority over other agents without a fair benchmark.

## Competition submission first
The preliminary entry requires project title, project introduction, application scenario, and technical solution. Prepare these before spending time on nonessential UI polish.

### Recommended title
Crows — A Local-First Agent for Verifiable, Approval-Gated Workflows

### Short introduction
Crows is a modular AI agent designed to turn user goals into inspectable, tool-assisted workflows while keeping execution accountable. It combines configurable model routes, workspace-scoped tools, durable task journals, dependency-aware orchestration, explicit memory, and approval-gated coding proposals. Interrupted or uncertain work is recorded for inspection rather than silently replayed. Optional isolated testing helps validate proposed code changes before a user approves them.

### Application scenario
A solo developer or small engineering team asks the agent to investigate a bug in an existing project. Crows inspects approved workspace files, proposes a bounded plan, invokes registered tools, records each tool outcome, prepares a reviewable code diff, optionally runs tests on a disposable snapshot, and waits for explicit approval before applying changes. If a provider or task fails, the journal exposes the failure and preserves uncertain outcomes for deliberate recovery.

### Technical solution
- FastAPI service and lightweight UI, intended to run locally.
- Configurable OpenAI-compatible primary, coding, reasoning, and fallback routes with explicit auto/manual routing.
- Registered native tool calls with bounded arguments and execution caps.
- SQLite-backed tasks, tool executions, chat sessions, memories, and workflow checkpoints.
- Dependency-aware specialist orchestration with bounded concurrency, QA checkpoint, and explicit recovery.
- Workspace-scoped file/web tools and path protections.
- Approval-gated coding proposals, stale-file checks, bounded test-and-fix, and optional Docker test isolation.
- Regression tests and GitHub Actions; the exact submission revision must pass CI and end-to-end acceptance checks.

Check every claim against the exact revision submitted. Do not claim all inference is local when a hosted provider is configured, and do not describe the current MVP as production-ready.

## Capability inventory
Status labels: “implemented (source-level)” means present in inspected code/docs, but still needs end-to-end acceptance testing; “partial / validate” means operational reliability, UI integration, or evidence is incomplete; “planned” means not a completed capability.

### Existing foundations — source-level implemented
- Local FastAPI service, API token protection, and same-origin checks for state-changing requests.
- Configurable OpenAI-compatible model routes and auto/manual mode.
- Native registered tool calling with bounded execution.
- Workspace file listing/reading/search and public web search/open tools.
- Persistent task/tool journal, sessions, explicit memory and search.
- Dependency-aware agent orchestration, bounded concurrency, QA checkpoint, and explicit recovery.
- Coding proposals with explicit approval, stale-state checks, and optional Docker-based tests.
- Automated test suite, competition submission notes, and demo runbook.

### Must validate before relying on it in a demo
- Native tool execution and clear failure explanations with real configured models.
- Provider fallback/retry behavior and cost/latency reporting.
- Recovery after actual process interruption at representative checkpoint boundaries.
- Coding proposal → review → approval → apply → test path.
- Authentication and mobile browser use.
- Vercel frontend integration with the intended backend; static frontend deployment does not itself host the local FastAPI agent.
- Clean-environment installation and reproducible demo.

### Desired capabilities — not all implemented
The long-term target includes browser automation; terminal/command execution; file operations; persistent memory; scheduled tasks; multi-step workflows; subagents and task delegation; tool integrations; messaging integrations; event hooks; a skills system; multi-agent/session routing; a central multi-channel gateway; mobile device nodes; channel/user-specific session routing; Lobster-style workflow pipelines; gateway-centric management; agent-managed skill creation and improvement; cross-session full-text search and recall; multiple terminal backends; programmatic tool calling (execute_code); batch processing and trajectory export; and Atropos RL-training integration.

These are roadmap items unless code and acceptance tests prove otherwise. Add them incrementally behind explicit capability boundaries and tests.

## Engineering priorities

### P0 — Release integrity and safety
1. Keep PR #230 (Fix safe native tool error feedback) open until tests and CI validate it. Inspect the final diff and verify sanitizer behavior; a Vercel status is only a frontend deployment check, not Python test evidence.
2. Run the full suite on the current branch and main; fix failures before stacking more work.
3. Add regression tests for native ToolError messages: actionable expected errors can reach the model, but credentials and host-specific paths are redacted; unexpected exceptions remain generic.
4. Verify task-journal and tool-execution status transitions, call caps, cancellation, and uncertain outcomes.
5. Confirm no secrets, API keys, tokens, local personal paths, or accidental backup files enter commits.
6. Produce release evidence: exact commit SHA, CI run, test count, acceptance gates, and known limitations.
Acceptance: CI green on the exact commit; test suite passes; sanitizer tests pass; no secrets in diff; existing safety invariants remain intact.

### P1 — One complete, impressive, reproducible agent workflow
1. Create a small non-sensitive sample workspace.
2. Demonstrate plan → registered tool use → journaled result → bounded specialist work → QA checkpoint → final summary.
3. Demonstrate a failure path and a truthful, actionable explanation.
4. Demonstrate coding proposal → diff review → explicit approval → apply → tests, with no file changes before approval.
5. Verify the UI exposes real states rather than simulated success.
6. Capture repeatable logs and evidence for the three-minute demo.
Acceptance: repeat the workflow on a clean checkout; document successes and failures; never present a mocked action as live execution.

### P2 — Measurable agent quality
Create a small versioned evaluation set covering file lookup, multi-file investigation, web research, tool failures, dependent subtasks, coding proposals, and recovery. Record task success/partial/failure; factual correctness and citation quality; correct tool selection and arguments; unauthorized action count (target zero); false-success count (target zero); recovery/replay safety; latency; model/provider; and estimated cost where available.

Compare Auto vs Manual routing and candidate models on the same tasks. Do not claim benchmark superiority until results are repeatable and the method is documented.

### P3 — Safer extensibility
1. Skills registry with manifest/schema, declared permissions, versioning, provenance, and disable/rollback controls.
2. Skills execute with least privilege and bounded resources; new or modified skills require review and tests before activation.
3. Typed tool contracts and consistent structured errors across every tool.
4. Workflow templates with explicit dependencies, deadlines, cancellation, idempotency policy, and per-step checkpoints.
5. Approval policies by action risk: read-only actions may run within scope; writes, external messages, shell commands, deployments, and destructive actions require explicit consent.
6. Audit log and export that omit secrets and distinguish model claims from verified tool results.

### P4 — Broader autonomy (only after P0–P3)
- Browser automation through a controlled browser adapter with domain policy, confirmation for consequential actions, and audit traces.
- Terminal execution through pluggable backends, isolated workspaces, command policy, time/resource limits, and approval for risky commands.
- Scheduled and event-triggered tasks with visible ownership, pause/disable, idempotency, bounded retries, and failure notifications.
- Messaging integrations behind per-channel permissions and user/session routing.
- Multi-agent gateway and mobile nodes with authenticated enrollment, revocation, scoped capabilities, and clear data-flow boundaries.
- Cross-session full-text retrieval with access controls, source references, retention/deletion behavior, and prompt-injection-resistant treatment of retrieved content.
- execute_code, batch/trajectory export, and optional RL-training integration only with sandboxing, data governance, and reproducible evaluation.

### P5 — Product experience and deployment
- Minimal responsive chat UI, reliable keyboard/touch behavior, session history, visible tool activity, cancel/retry, and readable error states.
- Never display API credentials in model listings, client logs, URLs, screenshots, or telemetry.
- Clearly separate static frontend hosting from the local/API backend. Do not expose a local agent API publicly without a separately reviewed auth, authorization, network, and deployment design.
- Add accessibility and mobile browser acceptance checks after core execution reliability is stable.

## Architecture guardrails

- A SQLite database has one active Crows FastAPI server process at a time. The process owns a dedicated OS lock for its full lifespan; a second server using the same database fails startup rather than marking live task/tool/workflow records as interrupted. Separate server instances must use separate databases. This lock is for local filesystems, not network filesystems.
- Keep planning, tool execution, durable state, approval, and final verification as distinct stages.
- Treat model output, web pages, workspace content, and retrieved memories as untrusted input, not privileged instructions.
- Validate every tool name and schema server-side. Bound argument/output size, recursion, call count, time, and concurrency.
- Do not automatically replay a task whose side-effect outcome is unknown.
- Use idempotency keys or explicit deduplication for operations that can cause duplicate external side effects.
- For coding, preserve original-state checks and explicit approval; test candidate changes in a disposable copy.
- Return useful, sanitized expected errors; hide raw unexpected exception details.
- Distinguish a model assertion from a verified result from a tool or test.
- Make recovery an explicit user-visible action when a previous outcome is uncertain.
- “Local-first” is not synonymous with “secure by default” unless network exposure, credentials, filesystem scope, and integrations are reviewed.

## Definition of done for every PR
- Scope is reviewable and has a clear user-visible or reliability benefit.
- Tests cover success, failure, boundaries, and security-relevant behavior.
- Existing tests and the full suite pass; CI result is linked or recorded.
- git diff --check is clean and no generated backups/secrets are included.
- Documentation matches actual behavior and states limitations.
- Do not call a PR complete merely because it was opened or frontend deployment succeeded.
- Do not merge until required checks are green and the change has been reviewed.

## Competition timeline (based on organizer details supplied by the project owner)
### 29 October – 2 November: reliability and evidence
- Fix P0 issues; verify CI and the full suite.
- Complete one end-to-end workflow and its failure path.
- Freeze core demo scope.

### 3–6 November: demo and evaluation
- Run the workflow on a clean setup repeatedly.
- Capture measurements and truthful evidence.
- Finalize title, introduction, scenario, and technical solution.
- Check portal fields and team/submission requirements.

### 7–9 November: submission review
- Review all claims against code and evidence.
- Remove secrets/personal data from docs, screenshots, and recording.
- Ensure code is open-source and setup instructions are reproducible.
- Submit before the portal deadline; do not wait until the final hour.

### 10 November: deadline
- Treat this as contingency time, not planned development time.
- Confirm portal receipt after submission.

## Final release checklist
- [ ] Preliminary entry submitted with all required fields.
- [ ] Exact commit SHA recorded.
- [ ] CI green on that SHA.
- [ ] Full test suite green on release candidate.
- [ ] Core workflow and failure path repeatedly reproduced.
- [ ] Coding changes remain untouched until explicit approval.
- [ ] Task/tool/workflow journal matches observed outcomes.
- [ ] No secrets or private data in repository/demo.
- [ ] Three-minute demo stays within time and contains no fake success.
- [ ] README, submission description, and video claims match shipped revision.
- [ ] Limitations and planned features are labelled honestly.