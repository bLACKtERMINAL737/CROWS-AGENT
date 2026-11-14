# Crows Demo Development Plan (15 November–20 December 2026)

## Objective

Deliver a repeatable, locally runnable Crows demonstration by **27 November 2026**, then use **28 November–20 December** only for bounded verification, essential fixes, documentation, and final packaging around the university term finals. The creative-work submission dated 10 November is complete; this plan covers the next demo-development stage.

The demo must prove a real end-to-end workflow, not just show a list of merged pull requests.

## Demo story

A user gives Crows a multi-step research-and-action goal. Crows:

1. Receives the goal in the local web UI.
2. Produces a bounded specialist plan and dependency graph.
3. Shows the exact plan for user review before execution.
4. Executes independent research tasks within the shared concurrency budget.
5. Records task checkpoints, tool activity, and workflow timeline.
6. Handles a controlled, deterministic specialist failure without pretending the task succeeded.
7. Blocks dependent work when prerequisites fail and preserves uncertain work as unknown rather than replaying it.
8. Shows the read-only recovery preview and durable recovery audit history.
9. Runs the final QA checkpoint and exposes the workflow evaluation.
10. Presents the final result, timeline, and audit evidence in the UI.

The controlled failure must be a safe test fixture or stubbed provider response. It must not depend on a live provider outage, external social account, or unpredictable network behavior.

## Scope lock

### Required for the demo
- One polished, scripted happy-path workflow.
- One deterministic failure/recovery-safety scenario.
- Plan review and explicit execution.
- Durable workflow/task checkpoints and timeline.
- Final QA/evaluation evidence.
- Clear setup, reset, run, and troubleshooting instructions.
- A clean local run with secrets and personal data excluded from recordings.

### Not required before the demo freeze
- New messaging platforms or a Discord Gateway listener.
- Autonomous skill authoring/self-improvement.
- Mobile apps/nodes.
- Video/voice generation.
- Public multi-user hosting or production deployment.
- Large architectural rewrites or speculative features.

Existing Telegram/Discord integrations may be shown only if they are stable and can be demonstrated without exposing credentials; they are not on the critical path.

## Milestones

| Dates | Focus | Exit criteria |
| --- | --- | --- |
| 15–17 Nov | Demo baseline and scope lock | Main branch health checked; exact scenario, test data, required endpoints, and known blockers documented |
| 18–20 Nov | Deterministic demo harness | Repeatable fixture data, controlled failure injection, database reset procedure, and smoke tests |
| 21–23 Nov | End-to-end workflow integration | Plan → approval → execution → failure handling → QA/evaluation works in a clean local run |
| 24–25 Nov | UI and observability | Demo UI surfaces plan, task progress, timeline, failure state, QA result, and audit history clearly |
| 26 Nov | Reliability pass | Repeated clean runs; no duplicate task execution; no accidental replay of unknown work |
| 27 Nov | Demo freeze | Tagged/recorded known-good commit, setup guide, screenshots/video capture checklist, and rollback point |
| 28 Nov–6 Dec | Light verification only | Short, scheduled smoke checks; fix only demo-blocking regressions |
| 7–13 Dec | Evidence and documentation | Final demo script, architecture explanation, limitations, test evidence, and FAQ |
| 14–17 Dec | Final rehearsal | At least two clean rehearsals from reset state; verify recording and backup path |
| 18–20 Dec | Buffer and handoff | Freeze changes, archive demo artifacts, verify instructions, and finish all project deliverables |

## Workload rules around term finals

- **15–27 November:** main engineering push. Work in focused batches with a working checkpoint at the end of each batch.
- **28 November–20 December:** university finals take priority. Keep Crows work to one short, planned maintenance block when needed; avoid starting broad features.
- No risky refactors after 27 November unless they fix a demo-blocking defect.
- Every change must have a narrow scope, regression test, and documented rollback path.
- Do not claim a milestone complete until the acceptance checks have actually been run.

## Acceptance checklist

- [ ] A fresh local database can be initialized from documented steps.
- [ ] The same demo input produces the expected plan structure.
- [ ] The plan is visible before execution and execution requires the reviewed plan.
- [ ] Independent tasks respect the configured concurrency limit.
- [ ] A controlled task failure is visible and dependent tasks are blocked.
- [ ] Uncertain tasks are not automatically replayed.
- [ ] Recovery preview is read-only and the audit trail is persisted.
- [ ] Final QA/evaluation is visible and accurately reflects the result.
- [ ] The demo can be repeated from a clean reset without manual database edits.
- [ ] Logs, screenshots, and recordings contain no API keys, tokens, private messages, or personal data.
- [ ] Known limitations are stated plainly; no unsupported production-readiness claims.

## Existing assets to reuse (already present on main)

- `docs/COMPETITION_DEMO_RUNBOOK.md` contains the local startup, review-first planning, UI, safety, and three-minute recording guidance.
- `examples/competition_demo/` contains the non-sensitive sample workspace fixture.
- `tests/test_agent_workflows.py` already covers persisted plans/checkpoints, specialist failure, dependency blocking, and concurrency limits.
- `tests/test_workflow_evaluation_api.py` already covers a successful deterministic evaluation response and content minimization.
- `tests/test_agent_workflow_recovery.py` already covers recovery preview and durable recovery-audit behavior.

These are starting assets, not proof that the complete demo has passed on the current release candidate. Extend them only where an integration gap is found; avoid duplicating existing fixtures, runbooks, or tests.

## Immediate next engineering batch

1. Run the current `main` CI and full local test suite; resolve existing blockers before layering demo changes.
2. Rehearse the existing `examples/competition_demo/` fixture and `docs/COMPETITION_DEMO_RUNBOOK.md` from a clean local database.
3. Identify the missing end-to-end link between review-first planning, deterministic task failure, workflow timeline, recovery audit, and evaluation.
4. Add only the missing integration regression coverage and a repeatable reset/run command if the existing fixture instructions are insufficient.
5. Polish the visible demo flow only after the above is reproducible.

## Status

- Creative-work submission: completed by the user on 10 November 2026.
- Demo development: starting now, ahead of the competition's scheduled 25 November demo-development window.
- Demo freeze target: 27 November 2026.
- Final completion target: 20 December 2026.
- Current implementation status: planning baseline committed separately; engineering acceptance items remain unchecked until verified.
