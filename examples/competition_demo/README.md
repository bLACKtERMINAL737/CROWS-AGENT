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
