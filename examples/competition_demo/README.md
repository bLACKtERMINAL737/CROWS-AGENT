# Crows competition demo fixture

This small, non-sensitive sample project is intended for the local competition demo only.

## Prepare a disposable workspace

From the repository root, copy this directory into the configured workspace (default: `./workspace`):

```bash
mkdir -p workspace
cp examples/competition_demo/known_facts.txt workspace/
cp examples/competition_demo/calculator.py workspace/
cp examples/competition_demo/test_calculator.py workspace/
```

If your `CROWS_WORKSPACE` points somewhere else, copy the three files there instead. Do not overwrite real project files.

## Suggested checks

1. Ask Crows to read `known_facts.txt` and report both unique lines. Confirm the task journal records `workspace.read_file`.
2. Ask it to inspect `calculator.py` and propose a small correction or improvement. Review the diff and verify the file is unchanged before approval.
3. If using Docker-backed testing, run the test runner against this workspace and show the actual result. The included test is a tiny smoke test, not evidence of broad project correctness.

Only present a step in the video after observing it succeed on the release candidate.


## Primary agent-workflow demo goal

Use this bounded prompt in the Agent Workflows panel after preparing the
workspace:

> Review the sample workspace. Read known_facts.txt and calculator.py,
> report the two verified facts from the text file, inspect the calculator's
> behavior, and produce a concise evidence-based review. Do not modify files,
> run shell commands, or send external messages. Cite the tool results used.

Generate a plan preview first. Inspect the specialist roles and dependencies,
then explicitly execute the exact reviewed plan. In the task journal, confirm
that the file contents came from a successful workspace.read_file tool
execution. Finish by checking the workflow timeline, QA result, and evaluation.

This is a live-provider workflow: only record it as successful after observing
the actual tool call and returned content on the target machine. The automated
workflow tests use deterministic fixtures and are regression evidence, not a
substitute for this live acceptance run.
