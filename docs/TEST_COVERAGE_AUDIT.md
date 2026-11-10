# Test Coverage Audit

Audit baseline: `main` after merge `02187be` (4 November 2026).

This document distinguishes automated regression coverage from end-to-end acceptance evidence. A passing pytest suite does not, by itself, prove that every advertised capability works in a clean installation or with live external providers.

## Coverage visible in the repository

| Area | Existing automated coverage | Remaining validation |
|---|---|---|
| API protection | Token enforcement, loopback fallback, same-origin checks, malformed/duplicate headers, body-size limits, protected docs | Exercise sensitive export endpoints through the real ASGI app; verify authenticated and unauthenticated behavior |
| Workspace and native tools | Workspace listing/search, path-kind validation, symlink boundaries, native tool contracts and journaling | Clean-install acceptance with representative workspace tasks and a real configured provider |
| Coding proposals | Approval gates, stale-file checks, symlink/permission races, concurrent apply claims, rollback and process-crash recovery | Rehearse the complete proposal → review → approval → apply → sandbox-test flow on a clean checkout |
| Agent workflows | Plan validation, specialist tool journaling, QA/evaluation, pause/resume/cancel and recovery scenarios | Process-level interruption at representative workflow checkpoints with outcome evidence |
| Models and routing | Route selection, manual fallback controls, provider context and failure paths | Live provider fallback/retry acceptance and measured latency/cost on repeatable tasks |
| Memory and sessions | Persistence, opt-in recall, cross-session search, export document shape and conversation history | Verify protected export routes end-to-end; test backup/restore procedures only if an import capability is implemented |
| Scheduling | Schedule API, idempotency, leases, worker lifecycle, cancellation and reconciliation | Validate time-zone/DST behavior and restart recovery on supported platforms |
| Messaging | Telegram/Discord validation, inbound dispatch, session routing, outbox, retries, unknown outcomes and reconciliation | Live provider acceptance with controlled test accounts; do not treat mocked transports as delivery proof |
| Sandbox | Snapshot limits, secret exclusions, resource limits and failure boundaries | Docker build/runtime acceptance on each supported host/architecture |
| UI | Static UI contracts and API-level panel tests | Browser-based smoke tests for critical flows and mobile layout |

## High-priority gaps

1. **Sensitive export endpoint integration tests.** Existing export tests validate helper output and route shape, but do not exercise both export endpoints through the application's authentication middleware. This audit adds that regression coverage.
2. **Live end-to-end acceptance.** Mocked providers and unit tests cannot establish real-provider reliability, actual delivery, or real-model tool-use quality.
3. **Clean-environment install.** Verify installation and startup from a fresh checkout without relying on the developer's existing virtual environment or local state.
4. **Windows platform compatibility.** The CI workflow now runs Ubuntu 24.04, macOS 15, and Windows. PR #335 added Windows-specific workspace, coding-apply, SQLite initialization, timestamp-ordering, and test portability fixes; its Tests workflow completed successfully across the matrix. Keep platform-specific regression tests in place as code evolves.

## Not implemented (do not describe these as test gaps)

The master roadmap identifies browser automation, general terminal/command execution, video/voice processing, mobile device nodes, multi-agent gateway expansion, and training integrations as future capabilities. They need implementation and explicit acceptance criteria before tests can meaningfully be added.

## How to use this audit

- Add a regression test when implemented behavior lacks a test.
- Add an acceptance test when unit tests exist but the integrated user workflow is unverified.
- Treat Windows support as CI-validated for the current matrix, not as proof that every optional dependency or Docker-backed workflow is supported on Windows.
- Track live-provider and Docker checks separately from deterministic CI tests.
- Re-run the full suite after each batch and record the exact commit and CI result.
- Update this matrix as capabilities change; do not equate test count with product completeness.
