#!/usr/bin/env python3
"""Run a privacy-conscious, repeatable evaluation set against a local Crows API.

Example:
  CROWS_API_TOKEN=... python scripts/agent_eval.py \
    --cases examples/agent_eval_cases.json \
    --base-url http://127.0.0.1:8000 --output agent-eval-report.json

The report intentionally excludes prompts, answers, headers, and credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load and validate a JSON array of evaluation cases."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read evaluation cases: {type(exc).__name__}") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("Evaluation file must contain a non-empty JSON array")
    names: set[str] = set()
    for index, case in enumerate(payload):
        if not isinstance(case, dict):
            raise ValueError(f"Case {index} must be an object")
        name = case.get("name")
        message = case.get("message")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError(f"Case {index} needs a name of 1–120 characters")
        if name in names:
            raise ValueError(f"Duplicate case name: {name}")
        names.add(name)
        if not isinstance(message, str) or not message.strip() or len(message) > 20_000:
            raise ValueError(f"Case {name!r} needs a message of 1–20000 characters")
        expected = case.get("expected_contains", [])
        forbidden = case.get("expected_not_contains", [])
        tools = case.get("expected_tools", [])
        if not isinstance(expected, list) or any(not isinstance(item, str) or not item.strip() for item in expected):
            raise ValueError(f"Case {name!r}: expected_contains must be a list of non-empty strings")
        if not isinstance(forbidden, list) or any(not isinstance(item, str) or not item.strip() for item in forbidden):
            raise ValueError(f"Case {name!r}: expected_not_contains must be a list of non-empty strings")
        if not isinstance(tools, list) or any(not isinstance(item, str) or not item.strip() for item in tools):
            raise ValueError(f"Case {name!r}: expected_tools must be a list of non-empty strings")
        expected_calls = case.get("expected_tool_calls", [])
        if not isinstance(expected_calls, list):
            raise ValueError(f"Case {name!r}: expected_tool_calls must be a list")
        for call_index, call in enumerate(expected_calls):
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("name"), str)
                or not call["name"].strip()
                or not isinstance(call.get("arguments"), dict)
            ):
                raise ValueError(
                    f"Case {name!r}: expected_tool_calls[{call_index}] needs a name and arguments object"
                )
        expected_facts = case.get("expected_tool_facts", [])
        if not isinstance(expected_facts, list):
            raise ValueError(f"Case {name!r}: expected_tool_facts must be a list")
        for fact_index, fact in enumerate(expected_facts):
            if (
                not isinstance(fact, dict)
                or not isinstance(fact.get("tool_name"), str)
                or not fact["tool_name"].strip()
                or not isinstance(fact.get("value"), str)
                or not fact["value"].strip()
            ):
                raise ValueError(
                    f"Case {name!r}: expected_tool_facts[{fact_index}] needs a tool_name and non-empty value"
                )
        min_calls = case.get("min_tool_calls", 0)
        if type(min_calls) is not int or not 0 <= min_calls <= 5:
            raise ValueError(f"Case {name!r}: min_tool_calls must be an integer from 0 to 5")
        expected_status = case.get("expected_task_status", "succeeded")
        if not isinstance(expected_status, str) or expected_status not in {
            "succeeded", "failed", "unknown", "not_created"
        }:
            raise ValueError(f"Case {name!r}: invalid expected_task_status")
        mode = case.get("mode", "auto")
        if not isinstance(mode, str) or mode not in {"auto", "manual"}:
            raise ValueError(f"Case {name!r}: mode must be auto or manual")
        task_type = case.get("task_type", "auto")
        if not isinstance(task_type, str) or task_type not in {
            "auto", "general", "coding", "reasoning"
        }:
            raise ValueError(f"Case {name!r}: invalid task_type")
        if case.get("mode", "auto") == "manual" and case.get("model_route") not in {
            "primary", "coding", "reasoning", "fallback"
        }:
            raise ValueError(f"Case {name!r}: manual mode requires a valid model_route")
    return payload


def _request_json(
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Do not echo response bodies; they may contain provider or user details.
        raise RuntimeError(f"API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"API request failed ({type(exc).__name__})") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("API returned invalid JSON") from None
    if not isinstance(decoded, dict):
        raise RuntimeError("API returned an unexpected JSON shape")
    return decoded


def response_metadata(response: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded, non-secret routing metadata from a chat response."""
    metadata: dict[str, Any] = {"mode": case.get("mode", "auto")}
    route = response.get("route")
    if isinstance(route, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", route):
        metadata["route"] = route
    model = response.get("model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}", model):
        metadata["model"] = model
    fallback_used = response.get("fallback_used")
    if isinstance(fallback_used, bool):
        metadata["fallback_used"] = fallback_used
    attempts = response.get("attempts")
    if type(attempts) is int and 1 <= attempts <= 100:
        metadata["attempts"] = attempts
    return metadata


def run_case(
    case: dict[str, Any],
    *,
    base_url: str,
    token: str,
    timeout: float,
) -> dict[str, Any]:
    """Run one case and return only non-sensitive evaluation metadata."""
    payload = {
        "message": case["message"],
        "mode": case.get("mode", "auto"),
        "task_type": case.get("task_type", "auto"),
        "allow_fallback": case.get("allow_fallback", True),
        "use_saved_memories": case.get("use_saved_memories", False),
    }
    if case.get("mode", "auto") == "manual":
        payload["model_route"] = case["model_route"]
    started = time.monotonic()
    checks: list[str] = []
    tool_names: list[str] = []
    observed_tool_calls: list[dict[str, Any]] = []
    observed_tool_results: list[dict[str, Any]] = []
    task_status = "not_created"
    tool_calls = 0
    routing_metadata: dict[str, Any] = {"mode": case.get("mode", "auto")}
    try:
        response = _request_json(
            base_url.rstrip("/") + "/chat", token=token, payload=payload, timeout=timeout
        )
        routing_metadata = response_metadata(response, case)
        raw_answer = response.get("answer", "")
        if not isinstance(raw_answer, str):
            checks.append("invalid_answer_type")
            answer = ""
        else:
            answer = raw_answer
            if not answer:
                checks.append("empty_answer")
        normalized_answer = answer.casefold()
        expected = [item.casefold() for item in case.get("expected_contains", [])]
        if any(item not in normalized_answer for item in expected):
            checks.append("expected_text_missing")
        forbidden = [item.casefold() for item in case.get("expected_not_contains", [])]
        if any(item in normalized_answer for item in forbidden):
            checks.append("forbidden_text_present")
        raw_tool_calls = response.get("tool_calls", 0)
        if type(raw_tool_calls) is not int or raw_tool_calls < 0:
            checks.append("invalid_tool_call_count")
            tool_calls = 0
        else:
            tool_calls = raw_tool_calls
        raw_task_id = response.get("task_id")
        task_id = raw_task_id
        if task_id is not None and (
            not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 256
        ):
            checks.append("invalid_task_id")
            task_id = None
        if task_id is not None:
            try:
                task_url = base_url.rstrip("/") + "/tasks/" + quote(task_id, safe="")
                task = _request_json(
                    task_url,
                    token=token,
                    timeout=timeout,
                )
                task_status = str(task.get("status", "unknown"))
                executions = task.get("tool_executions", [])
                if isinstance(executions, list):
                    for execution in executions:
                        if not isinstance(execution, dict):
                            continue
                        name = str(execution.get("tool_name") or execution.get("name") or "")
                        if name:
                            tool_names.append(name)
                        raw_arguments = execution.get("input_json", execution.get("arguments"))
                        if isinstance(raw_arguments, str):
                            try:
                                raw_arguments = json.loads(raw_arguments)
                            except json.JSONDecodeError:
                                raw_arguments = None
                        if name:
                            observed_tool_calls.append({
                                "name": name,
                                "arguments": raw_arguments if isinstance(raw_arguments, dict) else None,
                            })
                            raw_result = execution.get("result_json", execution.get("result"))
                            if isinstance(raw_result, str):
                                try:
                                    raw_result = json.loads(raw_result)
                                except json.JSONDecodeError:
                                    raw_result = None
                            observed_tool_results.append({
                                "name": name,
                                "status": execution.get("status"),
                                "result": raw_result,
                            })
                expected_status = case.get("expected_task_status", "succeeded")
                if task_status != expected_status:
                    checks.append("unexpected_task_status")
            except RuntimeError:
                task_status = "unavailable"
                checks.append("task_journal_unavailable")
        elif raw_task_id is None and (case.get("expected_tools") or case.get("min_tool_calls", 0)):
            checks.append("task_id_missing")
        if len(tool_names) < case.get("min_tool_calls", 0):
            checks.append("tool_attempt_count_below_minimum")
        actual_tools = set(tool_names)
        if any(expected_tool not in actual_tools for expected_tool in case.get("expected_tools", [])):
            checks.append("expected_tool_missing")
        expected_calls = case.get("expected_tool_calls", [])
        if expected_calls and observed_tool_calls != expected_calls:
            checks.append("tool_call_arguments_mismatch")
        for fact in case.get("expected_tool_facts", []):
            matching = [
                item for item in observed_tool_results
                if item["name"] == fact["tool_name"] and item["status"] == "succeeded"
            ]
            if not matching:
                checks.append("expected_tool_result_missing")
                continue
            try:
                source_text = json.dumps(
                    [item["result"] for item in matching],
                    ensure_ascii=False,
                    sort_keys=True,
                ).casefold()
            except (TypeError, ValueError):
                source_text = ""
            expected_value = fact["value"].casefold()
            if expected_value not in source_text:
                checks.append("tool_result_missing_expected_fact")
            if expected_value not in normalized_answer:
                checks.append("answer_not_grounded_in_tool_result")
        return {
            "name": case["name"],
            "passed": not checks,
            "checks_failed": checks,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "task_status": task_status,
            "tool_calls": tool_calls,
            "tools_observed": sorted(set(tool_names)),
            **routing_metadata,
        }
    except RuntimeError as exc:
        return {
            "name": case["name"],
            "passed": False,
            "checks_failed": [str(exc)],
            "latency_ms": round((time.monotonic() - started) * 1000),
            "task_status": task_status,
            "tool_calls": tool_calls,
            "tools_observed": sorted(set(tool_names)),
            **routing_metadata,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True, help="JSON evaluation case file")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Crows API URL")
    parser.add_argument("--output", type=Path, default=Path("agent-eval-report.json"))
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()
    if not 0.1 <= args.timeout <= 600:
        parser.error("--timeout must be between 0.1 and 600 seconds")
    token = os.getenv("CROWS_API_TOKEN", "") or os.getenv("AETHER_API_TOKEN", "")
    try:
        cases = load_cases(args.cases)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    results = [
        run_case(case, base_url=args.base_url, token=token, timeout=args.timeout)
        for case in cases
    ]
    passed = sum(bool(item["passed"]) for item in results)
    report = {
        "schema_version": 1,
        "case_count": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 4),
        "results": results,
        "privacy_note": "Prompts, answers, request headers, and credentials are excluded.",
    }
    try:
        args.output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            f"Could not write evaluation report ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    print(f"Agent evaluation: {passed}/{len(results)} passed; report: {args.output}")
    for item in results:
        status = "PASS" if item["passed"] else "FAIL"
        detail = ", ".join(item["checks_failed"]) if item["checks_failed"] else "all checks passed"
        print(f"{status} {item['name']} ({item['latency_ms']} ms): {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
