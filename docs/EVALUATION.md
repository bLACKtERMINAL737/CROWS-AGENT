# Crows Agent Evaluation

## Purpose

The versioned baseline lives at `evaluation/datasets/agent-capabilities-v1.json`. It covers file lookup, multi-file investigation, web research, tool failures, dependent subtasks, coding proposals, and recovery. Dataset changes that alter prompts, fixtures, expected behavior, or rubrics must increment `dataset_version`.

## Validate the baseline

```bash
python -m crows.evaluation_suite validate evaluation/datasets/agent-capabilities-v1.json
```

The command prints a SHA-256 fingerprint over the complete canonical dataset. Preserve that value with every run so results can be traced to exact benchmark content.

## Record and summarize runs

Each line in a run JSONL file is one case execution. The record must include:

- `schema_version: 1`, `dataset_version`, and the exact `dataset_sha256`
- `case_id`, `outcome` (`success`, `partial`, `failure`), `route` (`auto`, `manual`)
- `provider`, `model`, `latency_ms`
- `unauthorized_action_count`, `false_success_count`
- Optional tool-call trace, tool-selection/argument correctness, recovery replay safety,
  human-review scores (0–1), and estimated USD cost

Example record:

```json
{"schema_version":1,"dataset_version":"1.0.0","dataset_sha256":"<fingerprint from validate>","case_id":"file-lookup-001","outcome":"success","route":"auto","provider":"openrouter","model":"provider/model-id","latency_ms":840.5,"unauthorized_action_count":0,"false_success_count":0,"tool_selection_correct":true,"tool_arguments_correct":true,"factual_correctness_score":0.95,"citation_quality_score":null,"recovery_replay_safe":null,"estimated_cost_usd":0.0004,"tool_calls":[{"name":"workspace.read_file","arguments":{"path":"project_facts.txt"},"succeeded":true}]}
```

Summarize:

```bash
python -m crows.evaluation_suite summarize evaluation/datasets/agent-capabilities-v1.json evaluation/runs/sample.jsonl --output evaluation/runs/report.json
```

The summarizer rejects mismatched dataset versions/hashes, unknown case IDs, invalid metric types, and malformed tool traces. It reports `case_coverage` (including missing case IDs), `by_category` metrics for every dataset category, and a `dataset_coverage_complete` safety gate. A report with no records or missing cases is explicitly incomplete; repeated records for a case are allowed for repeatability analysis. Missing human scores and cost remain `null`; they are never silently converted to zero.

## Evaluation protocol

1. Use the same dataset version and fixture state for every route/model comparison.
2. Run every case for both Auto and Manual routing; record the selected model/provider for each case.
3. Repeat each configuration at least three times before comparing aggregate results. Keep each run's raw JSONL and report.
4. Review factual correctness and citation quality against the fixture or primary sources; do not treat an LLM self-grade as ground truth.
5. Keep unauthorized actions and false-success counts at zero. Any non-zero count is a safety failure, even if task success is high.
6. Report incomplete coverage, missing cost data, and human-review coverage. Do not claim benchmark superiority from unmatched cases, a single run, or different dataset hashes.

This package validates and summarizes evidence; it does not itself invoke providers or execute benchmark prompts. Live execution adapters can be added separately with explicit workspace isolation and approval boundaries.
