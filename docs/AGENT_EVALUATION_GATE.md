# Agent evaluation regression gate

The comparison utility describes differences between runs. The regression gate adds a CI-friendly pass/fail decision against a known baseline.

## Run it

Create two reports using the same evaluation case file, workspace fixtures, and model configuration:

```bash
python scripts/agent_eval.py --cases examples/agent_eval_cases.json --output baseline.json
# After a code or routing change, run the same cases again:
python scripts/agent_eval.py --cases examples/agent_eval_cases.json --output candidate.json
python scripts/agent_eval_gate.py baseline=baseline.json candidate=candidate.json \
  --max-pass-rate-drop 0 \
  --max-latency-increase-percent 25 \
  --output agent-eval-gate.json
```

The default gate allows no pass-rate drop and allows up to a 25% increase in median case latency. Set a small pass-rate tolerance when the evaluation set is intentionally noisy. Latency is descriptive and should be measured with repeated, comparable runs. If baseline median latency is zero, the percentage check is omitted rather than dividing by zero.

The gate requires identical case-name sets so accidental dataset changes cannot masquerade as a regression. It exits with code 0 when thresholds pass, 1 when a regression is detected, and 2 for invalid reports or arguments. The JSON output contains aggregate counts and timing only; it excludes case names, prompts, answers, tool arguments, raw tool results, and credentials.

Use a stable baseline report produced from a trusted revision and retain it as a CI artifact or controlled benchmark input. Do not commit reports containing private project timing or identifiers. This is a practical regression signal, not a statistically rigorous model benchmark; repeat runs and investigate failures in the local task journal.
