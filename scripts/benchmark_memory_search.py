#!/usr/bin/env python3
"""Reproducible, non-gating benchmark for SQLite-backed memory search.

Run from the repository root:
    python scripts/benchmark_memory_search.py
    python scripts/benchmark_memory_search.py --sizes 1000 10000 50000 --iterations 50

The script creates temporary databases, seeds them in batches, verifies each
search result, and reports latency percentiles as JSON. Timings are diagnostic;
they are deliberately not CI pass/fail thresholds.
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
import uuid
from pathlib import Path

from crows.store import TaskStore, _memory_search_tokens


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return ordered[index]


def seed_store(store: TaskStore, memory_count: int) -> str:
    target_id = str(uuid.UUID(int=1))
    now = "2026-01-01T00:00:00+00:00"
    memories = []
    terms = []
    for index in range(memory_count):
        memory_id = target_id if index == 0 else str(uuid.UUID(int=index + 1))
        content = (
            "CROWS_BENCHMARK_NEEDLE unique searchable target"
            if index == 0
            else f"Unrelated archive note number {index} with routine project details"
        )
        memories.append(
            (memory_id, "benchmark", content, "benchmark", "[]", now, now)
        )
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
    return target_id


def benchmark_size(memory_count: int, iterations: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="crows-memory-bench-") as temp_dir:
        store = TaskStore(Path(temp_dir) / "benchmark.sqlite3")
        expected_id = seed_store(store, memory_count)

        query = "CROWS_BENCHMARK_NEEDLE"
        for _ in range(5):
            results = store.search_memories(query, limit=5)
            if not results or results[0]["id"] != expected_id:
                raise RuntimeError(
                    f"Search correctness check failed for dataset size {memory_count}"
                )

        samples_ms = []
        for _ in range(iterations):
            started = time.perf_counter()
            results = store.search_memories(query, limit=5)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if not results or results[0]["id"] != expected_id:
                raise RuntimeError(
                    f"Search result changed during benchmark at size {memory_count}"
                )
            samples_ms.append(elapsed_ms)

        return {
            "memory_count": memory_count,
            "iterations": iterations,
            "p50_ms": round(statistics.median(samples_ms), 3),
            "p95_ms": round(percentile(samples_ms, 0.95), 3),
            "min_ms": round(min(samples_ms), 3),
            "max_ms": round(max(samples_ms), 3),
            "result_verified": True,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[100, 1000, 10000],
        help="memory dataset sizes to benchmark",
    )
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()

    if args.iterations < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and iterations must be positive integers")

    report = {
        "benchmark": "crows_memory_search",
        "query": "CROWS_BENCHMARK_NEEDLE",
        "results": [
            benchmark_size(size, args.iterations) for size in args.sizes
        ],
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
