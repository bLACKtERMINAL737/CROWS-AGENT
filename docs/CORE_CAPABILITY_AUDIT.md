# Crows — Core Capability Audit

Audit baseline: `main` at merge commit `d7ac510` (PR #220).

This is a source-level scope audit, not a claim that every capability has been
validated in a live deployment. CI passing proves the automated suite passed;
it does not prove provider credentials, external services, Vercel deployment,
or every end-to-end user journey work in production.

## Implemented in the repository

- FastAPI local service, health/database checks, API token and same-origin protections.
- OpenAI-compatible provider routes for primary, coding, reasoning, and fallback models; auto/manual routing and bounded provider retries.
- Native tool calling with registered tool validation and journaled tool execution.
- Workspace listing, reading, text search, web search/open, and path/symlink protections.
- SQLite task journal, persistent chat sessions, explicit memory, memory/session search.
- Bounded agent planning/orchestration, dependency-aware task scheduling, concurrency limits, QA review checkpoints, and explicit recovery that avoids replaying uncertain work.
- Coding proposals with explicit approval and optional isolated Docker test execution.
- GitHub Actions automated test suite and competition demo runbook/script documents.

## Partial / must be validated end-to-end

- Multi-provider routing and fallback behavior with real configured providers.
- Browser/web research reliability across redirects, changing pages, and provider failures.
- Recovery after real process termination at each checkpoint boundary.
- Mobile UI and frontend-to-local-backend connectivity, including authentication behavior.
- The full demo journey on the target Mac with actual provider configuration and Docker.
- Quality, latency, and cost comparisons across model routes.

## Explicitly not a completed capability

- Benchmark-driven automatic cost/latency optimization and dynamic provider catalog.
- General-purpose arbitrary terminal/shell execution as a user-facing agent capability.
- Production-grade public hosting; the documented deployment model is local-first and the API should not be exposed publicly.
- A fully autonomous, self-improving skills ecosystem, multi-channel gateway, mobile device nodes, and RL-training integration.

## Priority order

1. Keep workflow outcomes and diagnostics consistent after both normal execution and recovery.
2. Expand end-to-end regression tests around timeout, cancellation, dependency blocking, and QA failure.
3. Validate the real demo path (provider config → chat/tool call → workflow journal → coding proposal/approval → sandbox tests).
4. Only then prioritize larger missing capabilities such as skill lifecycle and messaging integrations.

## Acceptance checklist

- [ ] Main CI is green after each merged change.
- [ ] Recovery diagnostics match final task and QA checkpoint states.
- [ ] No uncertain task is silently replayed.
- [ ] Provider credentials and local paths are absent from API diagnostics.
- [ ] Demo runbook succeeds on a clean environment using non-production test credentials.
- [ ] Mobile layout and auth flows are tested on real devices/browsers.
