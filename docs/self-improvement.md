# Crows Self-Improvement Engine: Safe MVP

## Integrated workflow

The iterative coding workflow runs the sandbox test suite against an untouched disposable baseline before proposing changes. Baseline evidence must pass and contain a recognizable pytest summary. Each candidate must pass and report at least as many tests as the baseline before a normal pending proposal can be created.

## Safety boundaries

- Missing, ambiguous, timed-out, or failed baseline evidence blocks iteration.
- Candidate failures, incomplete summaries, or lower test counts block promotion.
- Tests run in the existing network-isolated disposable Docker sandbox.
- Passing tests permit review only; the resulting proposal remains pending and requires explicit human approval before apply.
- No automatic apply, merge, or production workspace mutation is performed by the evaluator.
- Passing tests are evidence, not proof of correctness or security.
