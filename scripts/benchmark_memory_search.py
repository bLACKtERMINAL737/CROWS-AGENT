#!/usr/bin/env python3
"""Reproducible, non-gating benchmarks for representative SQLite memory searches.

Run from the repository root:
    python scripts/benchmark_memory_search.py
    python scripts/benchmark_memory_search.py --sizes 1000 10000 50000 --iterations 50

Each workload uses a temporary database and verifies its results. EXPLAIN QUERY
PLAN runs the exact SQL statement used by TaskStore.search_memories(). Timings
are diagnostic and are not CI pass/fail thresholds.
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from crows.memory_search import MEMORY_SEARCH_RECALL_SQL, MEMORY_SEARCH_SQL
from crows.store import TaskStore, _memory_search_tokens


WORKLOADS = {
    "rare_term": ("CROWS_BENCHMARK_NEEDLE", "rare"),
    "common_term": ("common", "common"),
    "multi_term": ("multi_alpha multi_beta", "multi"),
    "no_match": ("CROWS_BENCHMARK_ABSENT", "none"),
    "recall_intent": ("What code did I ask you to remember?", "recall"),
}


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


def content_for(index: int, workload: str) -> str:
    if workload == "recall":
        return "CROWS_RECALL_INTENT_SECRET_7319" if index == 0 else (
            f"Unrelated archive note number {index} with routine project details"
        )
    if workload == "rare":
        return "CROWS_BENCHMARK_NEEDLE unique searchable target" if index == 0 else (
            f"Unrelated archive note number {index} with routine project details"
        )
    if workload == "common":
        return (
            f"common project archive note number {index}"
            if index % 10 < 7
            else f"unrelated archive note number {index}"
        )
    if workload == "multi":
        if index == 0:
            return "multi_alpha multi_beta exact two-term target"
        if index % 3 == 1:
            return f"multi_alpha single-term archive {index}"
        if index % 3 == 2:
            return f"multi_beta single-term archive {index}"
        return f"unrelated archive note {index}"
    return f"ordinary archive note number {index}"


def seed_store(store: TaskStore, memory_count: int, workload: str) -> str | None:
    target_id = str(uuid.UUID(int=1))
    now = "2026-01-01T00:00:00+00:00"
    memories = []
    terms = []
    for index in range(memory_count):
        memory_id = target_id if index == 0 else str(uuid.UUID(int=index + 1))
        content = content_for(index, workload)
        source = "explicit_chat_request" if workload == "recall" and index == 0 else "benchmark"
        memories.append((memory_id, "benchmark", content, source, "[]", now, now))
        terms.extend(
            (memory_id, token)
            for token in _memory_search_tokens("benchmark", content)
        )

    with store.connect() as con:
        con.executemany(
            "INSERT INTO memories(id,category,content,source,tags_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            memories,
        )
        con.executemany(
            "INSERT INTO memory_search_terms(memory_id,term) VALUES(?,?)",
            terms,
        )
    return target_id if workload in {"rare", "multi", "recall"} else None


def verify_results(
    results: list[dict[str, Any]], workload: str, memory_count: int, target_id: str | None
) -> bool:
    if workload == "rare":
        return bool(results) and results[0]["id"] == target_id and len(results) == 1
    if workload == "common":
        expected_count = sum(1 for index in range(memory_count) if index % 10 < 7)
        return len(results) == min(5, expected_count) and all(
            "common" in _memory_search_tokens(item["content"]) for item in results
        )
    if workload in {"multi", "recall"}:
        return bool(results) and results[0]["id"] == target_id
    return results == []


def explain_production_query(
    store: TaskStore, query: str, recall_intent: bool = False
) -> list[str]:
    query_terms = sorted(_memory_search_tokens(query))
    with store.connect() as con:
        con.execute(
            "CREATE TEMP TABLE query_memory_terms(term TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        con.executemany(
            "INSERT INTO query_memory_terms(term) VALUES(?)",
            [(term,) for term in query_terms],
        )
        rows = con.execute(
            "EXPLAIN QUERY PLAN "
            + (MEMORY_SEARCH_RECALL_SQL if recall_intent else MEMORY_SEARCH_SQL),
            (5,),
        ).fetchall()
    return [row["detail"] for row in rows]


def benchmark_workload(
    memory_count: int, iterations: int, workload_name: str, query: str, workload: str
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="crows-memory-bench-") as temp_dir:
        store = TaskStore(Path(temp_dir) / "benchmark.sqlite3")
        target_id = seed_store(store, memory_count, workload)
        recall_intent = workload == "recall"
        query_plan = explain_production_query(store, query, recall_intent=recall_intent)
        index_used = any(
            "SEARCH indexed USING COVERING INDEX idx_memory_search_terms_term" in detail
            for detail in query_plan
        )
        full_scan_avoided = not any(detail == "SCAN memories" for detail in query_plan)
        source_index_used = any(
            "SEARCH memories USING COVERING INDEX idx_memories_source_id" in detail
            for detail in query_plan
        )
        if not index_used or not full_scan_avoided or (recall_intent and not source_index_used):
            raise RuntimeError(
                f"Production query plan regression for {workload_name} "
                f"(term_index_used={index_used}, source_index_used={source_index_used}, "
                f"full_scan_avoided={full_scan_avoided}): "
                + "; ".join(query_plan)
            )

        for _ in range(5):
            results = store.search_memories(query, limit=5)
            if not verify_results(results, workload, memory_count, target_id):
                raise RuntimeError(
                    f"Search correctness check failed for {workload_name} at size {memory_count}"
                )

        samples_ms = []
        for _ in range(iterations):
            started = time.perf_counter()
            results = store.search_memories(query, limit=5)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if not verify_results(results, workload, memory_count, target_id):
                raise RuntimeError(
                    f"Search result changed during {workload_name} benchmark at size {memory_count}"
                )
            samples_ms.append(elapsed_ms)

        return {
            "workload": workload_name,
            "memory_count": memory_count,
            "iterations": iterations,
            "result_count": len(results),
            "p50_ms": round(statistics.median(samples_ms), 3),
            "p95_ms": round(percentile(samples_ms, 0.95), 3),
            "min_ms": round(min(samples_ms), 3),
            "max_ms": round(max(samples_ms), 3),
            "result_verified": True,
            "production_query_term_index_used": index_used,
            "full_memories_scan_avoided": full_scan_avoided,
            "recall_source_index_used": source_index_used if recall_intent else None,
            "query_plan": query_plan,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 1000, 10000])
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()
    if args.iterations < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and iterations must be positive integers")

    report = {
        "benchmark": "crows_memory_search",
        "results": [
            benchmark_workload(size, args.iterations, name, query, workload)
            for size in args.sizes
            for name, (query, workload) in WORKLOADS.items()
        ],
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
