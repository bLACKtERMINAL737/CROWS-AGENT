# OpenJiuwen Proposal Submission — Crows

> Working draft targeting submission completion by 29 October 2026. Re-check every item against the final recorded demo before submission.

## Project name
**Crows — a local-first AI agent for controlled, verifiable task execution**

## Overview
Crows is a modular AI-agent foundation designed to make AI-assisted work safer to inspect and recover. Instead of silently changing a project, it separates proposal from execution, records task and tool states in SQLite, and requires explicit user approval for code changes. It supports configurable OpenAI-compatible model routes, optional local-model fallback, workspace-scoped file tools, persistent chat sessions and explicit memory, and an optional Docker-based test runner.

This is an evolving MVP, not a production-ready autonomous agent. The submission focuses on a demonstrable developer workflow: propose a code change, validate it in an isolated disposable copy, review the diff, then decide whether to apply it.

## October 23–24 engineering task review

The three requested coding-safety tasks are implemented on the repository's current default branch and have regression tests:

- [x] **Schema migration:** legacy `coding_proposals` schemas gain the `applying` status while existing proposal records and metadata are preserved.
- [x] **Atomic apply protection:** proposals are claimed through a conditional database transition, and a local OS file lock coordinates concurrent apply operations and startup recovery.
- [x] **Failure and rollback tests:** tests cover migration rollback/data preservation, replacement failure with rollback, rollback failure reporting, interrupted applies, concurrent apply requests, and failure to persist the final `applied` status.

The relevant test workflow has a successful GitHub Actions run on commit `fc09e6b`, which was subsequently merged as `8fe2125`. This is evidence of a passing CI run, but it does **not** replace a full test run on the exact final submission commit. Filesystem replacement and SQLite status updates cannot be one transaction; an interrupted or incomplete rollback remains flagged for manual reconciliation rather than being silently retried.

## Problem and target scenario
AI coding assistants can produce plausible edits, but developers still need to know what changed, whether tests pass, whether the intended files were touched, and what happens if execution is interrupted.

**Scenario:** a solo developer asks Crows to improve a small function in an existing local project. The agent reads selected workspace files, creates a reviewable proposal, and—when the optional Docker runner is enabled—tests the candidate against a disposable snapshot. A passing candidate remains pending for explicit approval; the original workspace is not silently overwritten by the test-and-fix loop.

## Technical approach

### 1. Local-first service and model routing
- FastAPI service intended to bind to loopback (`127.0.0.1`).
- Configurable primary, coding-specialist, optional reasoning-specialist, and fallback model routes using OpenAI-compatible APIs.
- Auto/manual route selection and bounded retries for selected transient provider failures.
- Provider credentials are configured through the process environment or in-memory route configuration; route discovery does not return credentials.

### 2. Durable task and session state
- SQLite stores task records, tool-execution records, coding proposals, sessions, messages, and explicit memories.
- Interrupted task/tool records are marked `unknown`, rather than blindly replayed when outcomes may be uncertain.
- Coding proposals use guarded state transitions, an apply claim, and a local OS file lock to reduce duplicate concurrent applies.
- Interrupted applies are flagged for manual workspace reconciliation rather than silently re-approved.

### 3. Review-first coding workflow
- The coding engine accepts a bounded list of existing workspace files and rejects path traversal, symlink paths, duplicate paths, and oversized inputs.
- Model output is validated and normalized into per-file changes and diffs.
- Changes are not applied until the user explicitly approves the proposal.
- Original file content hashes are checked to detect stale proposals.
- The iterative test-and-fix workflow works on a disposable copy, is bounded to at most three rounds, and returns a pending proposal only when the candidate passes.
- The optional Docker test runner uses a disposable snapshot with networking disabled and additional container restrictions. Docker is defense in depth, not a perfect security boundary.

### 4. Safety and limitations
- Local API protection supports a shared token and same-origin checks for state-changing requests.
- Workspace tools are scoped to the configured workspace.
- Saved-memory retrieval is opt-in per chat request; memory search is keyword-based in the current MVP.
- The project does not yet implement every feature of a fully autonomous agent: general browser automation, mobile nodes, a full multi-step workflow engine, a knowledge graph, and autonomous reinforcement learning are not completed features.
- Keep the service local; do not expose it to a public network.

## Updated release plan — October 25–29, 2026 (hard feature freeze)

| Date | Priority | Deliverable / exit condition |
|---|---|---|
| **Oct 25** | Confirm safety work | Review the schema migration, apply lock/claim, and failure/rollback regression coverage; preserve manual-review behavior for uncertain applies. No feature expansion needed for these tasks. |
| **Oct 26** | Reproducible verification | Pull the intended release branch into a clean checkout; run `python -m pytest -q` on the exact candidate commit and save the result. Investigate and fix any failure before proceeding. |
| **Oct 27** | Sandbox and demo rehearsal | Build the Docker image and run the sandbox against a disposable test project. Verify the real workspace stays unchanged until explicit approval. Do not claim sandbox execution if it has not been verified. |
| **Oct 28** | Submission quality review | Check setup instructions, scope/limitations, route configuration, secret redaction, and all demo claims against the running build. Freeze nonessential features. |
| **Oct 29** | Final submission deadline (internal target) | Submit the verified candidate, recorded demo, final commit SHA, test evidence, video URL, and required metadata. Verify portal confirmation and retain references. No feature changes after the final verification pass. |

### Demo plan (maximum 3 minutes)
Use a disposable test project and non-sensitive sample data. Never show API keys or tokens.

| Time | Show | Proof / narration |
|---|---|---|
| 0:00–0:20 | Introduce Crows and local health status | Local-first agent focused on controlled execution, explicit approval, and recoverable state. |
| 0:20–0:45 | Show configured model routes without secrets | Explain specialist routing and optional fallback. |
| 0:45–1:20 | Submit a small coding task and inspect proposal diff | Only selected files are eligible; proposal is pending review. |
| 1:20–1:55 | Run test-and-fix on a disposable test project | Show actual test result and state whether Docker is enabled. |
| 1:55–2:25 | Show pending proposal and approval boundary | Passing candidate is still not applied automatically. |
| 2:25–2:45 | Show database health or persisted task/session state | Pick one stable, verified screen. |
| 2:45–3:00 | Close with value proposition and MVP scope | “Propose, test, review, then apply—without pretending uncertain work succeeded.” |

If a step is not verified on the final commit, omit it rather than simulate success. Keep the exported video at or below 3:00.

## Submission checklist
- [ ] Confirm final branch/commit and clean working tree.
- [ ] Run the complete test suite on the exact submission commit; save the command and result.
- [ ] Build and verify the optional Docker sandbox if the demo claims sandbox execution.
- [ ] Record the demo from the actual running application; verify duration ≤ 3:00 and no secrets are visible.
- [ ] Review video and documentation for claims that exceed implemented behavior.
- [ ] Include repository link, overview/technical approach, demo video, and required submission metadata.
- [ ] Submit one entry before 30 October 2026; target 29 October to leave time for upload/validation.

## Final metadata to fill before submission
- Repository URL: https://github.com/bLACKtERMINAL737/CROWS-AGENT
- Exact submission commit: _fill after final code freeze_
- Test command and result: _fill after running on the exact submission commit_
- Demo video URL: _fill after recording and uploading_
- Submission portal confirmation: _fill after submission_
