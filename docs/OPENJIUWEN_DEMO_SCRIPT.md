# Crows — 3-minute demo recording checklist

## Before recording
- [ ] Checkout the exact submission candidate and record its commit SHA.
- [ ] Start the app locally; verify `GET /health` and `GET /health/db`.
- [ ] Verify model routes using `GET /models`; ensure no credentials appear in the capture.
- [ ] Prepare a disposable sample workspace with a tiny source file and tests.
- [ ] If demonstrating Docker tests, verify the sandbox image and test workflow before recording.
- [ ] Close unrelated tabs and never show keys, tokens, private paths, or personal data.
- [ ] Record a real run; do not imply a step succeeded if it did not.

## Spoken script and actions (target 2:50)

**0:00–0:20 — Why Crows**
“AI can write code quickly, but reliable agents must also show what they intend to change, verify the candidate, and handle uncertain outcomes. Crows is a local-first agent foundation built around those boundaries.”

**0:20–0:45 — Local configuration**
Show health status and configured route names only.
“The service runs locally, supports separate model routes, and keeps provider credentials out of route-discovery responses. Credentials are intentionally not displayed.”

**0:45–1:20 — Review-first proposal**
Submit one small coding instruction for explicitly selected files. Show the proposal summary and diff.
“The agent is limited to selected existing files. The result is a proposal—not an automatic write. Paths, output size, duplicate changes, and stale source files are checked.”

**1:20–1:55 — Verify in a disposable copy**
Run the test-and-fix flow against the disposable sample project. Show the actual test result.
“When the optional sandbox is enabled, the candidate is tested in a disposable copy. The loop is bounded, and a passing candidate remains pending review.”

**1:55–2:25 — Approval boundary**
Show pending state, then approve only if safe and intended for the demo. Show the applied state and resulting file if successful.
“The developer retains the final decision. Apply is separately approved, and the original file state is checked before the change is applied.”

**2:25–2:45 — Persistence and recovery**
Show database health or persisted task/session state.
“Task and tool outcomes are journaled. Interrupted work with an uncertain result is marked unknown instead of being blindly replayed.”

**2:45–3:00 — Close**
“Crows’s core principle is simple: propose, test, review, then apply. It is an evolving MVP, and this submission demonstrates safeguards implemented today.”

## After recording
- [ ] Verify exported duration is no more than 3:00.
- [ ] Watch the complete export and check legibility/audio.
- [ ] Confirm no credentials, tokens, private messages, or personal information are visible.
- [ ] Confirm every claim matches the actual recording and submission commit.
- [ ] Upload the video and place its final URL in `docs/OPENJIUWEN_SUBMISSION.md`.
