# Crows Architecture and Product Roadmap

## Product direction

Crows is a local-first, modular AI agent for reliable, reviewable task execution. The product should evolve into a dependable personal/work assistant that can plan work, use tools under explicit policy, retain useful context, and continue durable workflows across supported interfaces.

The design priority is **reliability before autonomy**. Crows should make progress visible, keep side effects bounded, and recover safely from interruptions rather than maximize the number of tools or agents.

## Architecture decision

Keep the near-term product as a **modular monolith with event-driven boundaries**. Do not split it into microservices until measured load, deployment isolation, or independent scaling requires that complexity.

```text
Interfaces
  Web UI | CLI | Telegram | Discord | future mobile/node clients
                         |
                API + Universal Gateway
        Auth | sessions | channel adapters | streaming
                         |
                  Agent Orchestrator
       plan | policy check | route | execute | verify
          /              |                \
   Model Router      Tool Runtime      Workflow Engine
   provider ports    browser/files     durable jobs
   local/cloud       terminal/code     schedules/retry
          \              |                /
                   Shared Context
          session memory | long-term memory | artifacts
                         |
               Persistence + Security
       SQLite first | migrations | audit | sandbox
```

### Existing foundations

The current repository already contains substantial pieces of this design:

- API, local UI, authentication, and session handling in the FastAPI application.
- Provider routing and task-specialist configuration.
- Registered tool execution, workspace boundaries, coding proposals, approval gates, and an optional Docker test runner.
- Persistent task journal and durable specialist workflows with pause/resume/cancel and recovery.
- Explicit saved memory and opt-in retrieval.
- Universal messaging gateway, inbound dispatch, session routing, outbound adapters, and durable delivery state.
- Cross-platform CI for supported Python versions and operating systems.

These are foundations, not a claim that every planned capability is production-ready. The README remains the source of truth for currently supported behavior and limitations.

## Architectural rules

1. **One orchestration path:** interfaces submit work to the same agent orchestration contract; channel adapters must not implement separate agent brains.
2. **Ports and adapters:** providers, tools, persistence, and messaging channels sit behind narrow interfaces so implementations can be replaced without rewriting orchestration.
3. **Policy before side effects:** validate identity, permissions, workspace scope, approval requirements, and idempotency before executing a tool.
4. **No blind replay:** if a provider or worker fails after a side effect may have happened, record an uncertain outcome and reconcile; do not automatically repeat the action.
5. **Durable state for long work:** persist task state, checkpoints, attempts, and outcomes before acknowledging durable workflow transitions.
6. **Memory is reference data:** retrieved memory is untrusted context, never executable instruction; keep explicit controls over what is stored and retrieved.
7. **Observable by default:** every task should expose status, selected model/route, tool activity, timing, retries, and a user-readable outcome without leaking credentials or private payloads.
8. **Local-first deployment:** core features should work on a local machine with SQLite; cloud providers and remote channels are optional integrations.
9. **Bounded autonomy:** require approval for consequential operations, enforce time/resource limits, and provide pause/cancel controls.
10. **Evidence-led complexity:** add subagents, services, vector databases, or new infrastructure only when a concrete evaluation demonstrates the need.

## Prioritized roadmap

### P0 — Make the current agent dependable

- Consolidate the request lifecycle into a clearly defined orchestrator: receive, classify, plan, authorize, execute, verify, and report.
- Make task execution traces inspectable: correlated task/run IDs, step events, tool outcomes, route/fallback decisions, and elapsed time.
- Strengthen idempotency and reconciliation for tools, workflows, and outbound messaging.
- Complete queue health metrics and worker lease observability; keep metrics aggregate and redact identifiers/secrets.
- Add deterministic failure-injection tests for provider timeouts, worker restarts, lease loss, partial tool completion, and duplicate delivery.
- Keep the README honest about supported and experimental capabilities.

**Exit criteria:** a demo task can be followed from request to final result; interruption does not silently duplicate a side effect; failures have actionable, inspectable states.

### P1 — Improve agent intelligence without losing control

- Introduce a typed, validated task plan with explicit dependencies, expected outputs, risk level, and required capabilities.
- Add a verification stage that checks task results against the requested outcome before reporting completion.
- Route tasks using capability, configured availability, latency/cost signals, and user-selected constraints; preserve a manual override.
- Add bounded context assembly: recent conversation, relevant memory, task artifacts, and tool results with provenance.
- Add specialist delegation only for tasks that benefit from parallel independent work; keep one coordinator responsible for final verification.

**Exit criteria:** the same evaluation set measures task success, tool correctness, cost, latency, and unsafe/duplicate side effects across releases.

### P2 — Make it a useful daily assistant

- Add reliable scheduling and recurring tasks with visible next-run state and cancellation.
- Add artifact handling for generated files and task outputs, with explicit workspace permissions.
- Improve web research and browser automation through a permissioned browser adapter and auditable actions.
- Add a consistent streaming/status experience across Web and messaging channels.
- Provide a clean onboarding flow for local models and OpenAI-compatible providers.

**Exit criteria:** a new user can install Crows, configure one provider, complete a safe multi-step task, inspect its work, and resume or cancel it.

### P3 — Expand modalities and deployment

- Add image understanding first, then voice input/output and video workflows based on measured demand.
- Add mobile/node clients as authenticated capability providers, not as a separate orchestration system.
- Consider PostgreSQL or external queue backends only when multi-user or multi-host deployment is a supported product goal.
- Add optional retrieval/vector indexing only when current memory search evaluations show a clear quality gap.

**Exit criteria:** each modality has end-to-end tests, privacy controls, clear resource limits, and a documented local deployment path.

## Evaluation strategy

Maintain a versioned evaluation set covering:

- task completion and final-answer correctness;
- correct tool selection and argument validation;
- policy enforcement and approval behavior;
- recovery after timeout, process restart, and lease expiry;
- duplicate-side-effect prevention;
- memory retrieval precision and cross-session relevance;
- provider fallback correctness;
- latency, token/cost use, and local resource consumption;
- usability of the Web and messaging-channel demo.

A feature is not complete because its endpoint exists. It is complete when its behavior is tested, observable, documented, and demonstrated through a realistic user workflow.

## Immediate next engineering sequence

1. Review and finish the existing durable queue observability work without duplicating current main-branch behavior.
2. Define a shared execution trace/event schema and connect it to chat, tools, and durable workflows.
3. Add end-to-end evaluation scenarios that exercise planning, tool execution, memory recall, and recovery together.
4. Use those evaluations to prioritize orchestrator improvements and model-routing changes.
5. Polish one coherent local demo path before expanding into additional modalities.

## Non-goals for the near term

- Premature microservice decomposition.
- Adding many channels before the shared gateway is reliable.
- Unbounded autonomous shell or browser access.
- Replacing working persistence layers without migration and recovery evidence.
- Marketing experimental features as production-ready.
