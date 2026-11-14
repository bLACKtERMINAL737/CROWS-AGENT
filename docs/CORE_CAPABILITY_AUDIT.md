# Crows — Core Capability Audit

Audit baseline: main at merge commit a46d449 (PR #415).

This is a source-level scope audit, not a claim that every capability has been
validated in a live deployment. CI passing proves the automated suite passed;
it does not prove provider credentials, external services, local Docker setup,
or every end-to-end user journey work in production.

## Implemented in the repository

- FastAPI local service, health/database checks, API token and same-origin protections.
- OpenAI-compatible provider routes for primary, coding, reasoning, and fallback models; auto/manual routing and bounded provider retries.
- Native tool calling with registered tool validation and journaled tool execution.
- Workspace listing, reading, text search, web search/open, and path/symlink protections.
- SQLite task journal, persistent chat sessions, explicit memory, memory/session search.
- Bounded agent planning/orchestration, dependency-aware task scheduling, concurrency limits, QA review checkpoints, and explicit recovery that avoids replaying uncertain work.
- Review-first workflow API: preview without creating tasks, then execute the exact reviewed plan.
- Workflow timeline, durable recovery audit, deterministic workflow evaluation, and persisted task outcomes.
- Coding proposals with explicit approval and optional isolated Docker test execution.
- Deterministic acceptance coverage for chat tool use, explicit memory, session persistence, reviewed workflow execution, QA, evaluation, and dependency-failure visibility.
- GitHub Actions automated test suite and workflow-first competition demo runbook.

## Partial / must be validated end-to-end

- Multi-provider routing and fallback behavior with real configured providers.
- Browser/web research reliability across redirects, changing pages, and provider failures.
- Recovery after real process termination at each checkpoint boundary.
- Mobile UI and frontend-to-local-backend connectivity, including authentication behavior.
- The full workflow-first demo on the target Mac with actual provider configuration and a prepared workspace.
- Coding proposal approval and Docker-backed testing on the target environment.
- Telegram/Discord inbound and outbound flows with real test credentials and live delivery.
- Scheduler worker behavior across real process restarts and delayed execution.
- Quality, latency, and cost comparisons across model routes.

## Explicitly not a completed capability

- Benchmark-driven automatic cost/latency optimization and dynamic provider catalog.
- General-purpose arbitrary terminal/shell execution as a user-facing agent capability.
- Production-grade public hosting; the documented deployment model is local-first and the API should not be exposed publicly.
- A fully autonomous, self-improving skills ecosystem, mobile device nodes, and RL-training integration.
- Universal support for every social platform or arbitrary multimedia input/output.

## Priority order

1. Validate the real agent path: provider configuration → chat/tool call → reviewed plan → specialist execution → persisted workflow/timeline → QA/evaluation.
2. Validate recovery after real process termination and keep uncertain work non-replayable without explicit inspection.
3. Validate coding proposal/approval and optional sandbox testing on the target machine.
4. Validate scheduler and messaging workers with real local configuration and test endpoints.
5. Only then prioritize larger missing capabilities such as additional skills, device nodes, and RL integration.

## Acceptance checklist

- [ ] Main CI is green after each merged change.
- [ ] Real configured provider completes a chat request on the target machine.
- [ ] Known-file request invokes workspace.read_file; the returned content matches the fixture.
- [ ] Reviewed workflow executes the exact previewed plan without replanning.
- [ ] Specialist task outcomes, dependencies, QA, timeline, and evaluation agree.
- [ ] A failed prerequisite blocks dependents; evaluation does not report a pass.
- [ ] Recovery diagnostics match final task and QA checkpoint states.
- [ ] No uncertain task is silently replayed.
- [ ] Provider credentials and sensitive local paths are absent from API diagnostics.
- [ ] Coding changes remain unapplied until explicit approval.
- [ ] Demo runbook succeeds on a clean environment using non-production test credentials.
- [ ] Mobile layout and auth flows are tested on real devices/browsers.
