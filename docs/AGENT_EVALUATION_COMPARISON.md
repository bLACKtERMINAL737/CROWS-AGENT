# Comparing Crows agent-evaluation runs

The evaluation runner produces one privacy-conscious JSON report per run. Use
the comparison utility to aggregate multiple Auto and Manual runs by the actual
mode, route, and model reported by the chat API.

## Run comparable experiments

1. Use the same disposable workspace, fixture files, case file, and provider
   configuration for each experiment.
2. Run the evaluation cases in Auto mode and save that report.
3. Run equivalent cases in Manual mode with explicit configured routes and save
   each report separately.
4. Repeat runs when provider latency or transient failures may vary.
5. Compare the reports:

```bash
python scripts/agent_eval_compare.py \
  auto-1=agent-eval-auto-1.json \
  auto-2=agent-eval-auto-2.json \
  manual-primary=agent-eval-manual-primary.json \
  manual-coding=agent-eval-manual-coding.json \
  --output agent-eval-comparison.json
```

The summary groups observations by mode, actual route, and model. It reports
case pass rate, mean and median latency, observed fallback rate, and mean
provider attempts when those fields were supplied by the API. Missing metadata
stays unknown; the utility does not infer provider cost or fill in missing
values.

The comparison report contains only aggregate evaluation metadata. It excludes
case prompts, model answers, tool arguments, credentials, and raw tool results.
Keep input and output reports private if they contain local timing or project
identifiers.

## Interpretation limits

Use identical case sets and comparable workspaces. A higher pass rate or lower
latency in a small sample is descriptive evidence, not proof of statistical
significance or general model superiority. Investigate individual failed cases
in the original local evaluation run and task journal; the aggregate report
intentionally does not include sensitive case-level content.
