# Reproducible Crows agent evaluation

The evaluation runner checks actual /chat responses against observable task-journal outcomes. It is intended for a local, non-sensitive workspace, not a public deployment.

## Setup

1. Start Crows on loopback and configure a working model route.
2. Set CROWS_API_TOKEN in the same shell used to run the evaluation if API protection is enabled. Never put the token in the case file or report.
3. Create the fixture in the configured workspace at eval-fixtures/known_fact.txt with this content:

```text
Crows evaluation marker: CROWS_EVAL_MARKER_9241
The sample project is named Northstar Demo.
```

4. Confirm that eval-fixtures/does-not-exist.txt does not exist. Use a disposable evaluation workspace; do not run this against a workspace containing private files.
5. Run from the repository root:

```bash
python scripts/agent_eval.py \
  --cases examples/agent_eval_cases.json \
  --base-url http://127.0.0.1:8000 \
  --output agent-eval-report.json
```

The report contains case names, pass/fail checks, latency, task status, successful tool-call count, and tool names observed in the journal. Cases may define `expected_not_contains` with non-empty strings; the runner performs case-insensitive negative assertions against the answer and fails when forbidden content appears. Cases may also define `expected_tool_facts` as `{tool_name, value}` assertions. Each value must appear in the successful matching tool's persisted `result_json` and in the assistant answer; a failed tool, missing result, or answer/source mismatch fails the case. Tool result values are compared locally and are never copied into reports. Use this for known fixture markers or facts that must not be invented after an expected tool failure. Answers and matched strings are never copied into the report. Cases may also define `expected_tool_calls` as an ordered list of exact `{name, arguments}` objects; the runner compares these against the persisted tool journal and fails on missing, extra, malformed, or mismatched calls. Argument values are used only for local comparison and are never copied into the report. It deliberately omits prompts, model answers, headers, credentials, and endpoint metadata. Keep the report out of version control if local project identifiers or timing data should remain private.

## Interpreting results

- The known-file case must observe workspace.read_file in the task journal and the exact marker in the answer.
- The missing-file case is an expected task failure: it passes evaluation only if the agent gives the expected honest response and the journal confirms the failed workspace.read_file call. A task marked failed is not treated as successful execution.
- A failing evaluation means the expected behavior was not observed; inspect the local UI and task journal before drawing conclusions.
- This is a starter evaluation set, not a benchmark proving general intelligence or superiority over other agents. Run multiple times with the same workspace and configuration before comparing models. Record model/provider and cost separately in a private experiment log if needed.

The runner makes real requests and can invoke real tools. Use only harmless fixture tasks and a local or otherwise trusted API endpoint.