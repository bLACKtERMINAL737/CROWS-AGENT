#!/usr/bin/env python3
"""Reproducible scale benchmark for indexed cross-session message search.

Run:
    python scripts/benchmark_session_search.py
    python scripts/benchmark_session_search.py --sizes 10000 50000 --iterations 10

Timings are diagnostic; result verification and query-plan checks are gating.
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


def seed_messages(store: TaskStore, count: int, workload: str) -> str | None:
    session = store.create_session("Cross-session benchmark")
    now = "2026-01-01T00:00:00+00:00"
    messages = []
    terms = []
    target_id = str(uuid.UUID(int=1))
    for index in range(count):
        message_id = target_id if index == 0 else str(uuid.UUID(int=index + 1))
        if workload == "rare":
            content = (
                "CROWS_SESSION_NEEDLE unique indexed target"
                if index == 0 else f"Routine archive message {index}"
            )
        elif workload == "common":
            content = (
                f"common archive message {index}"
                if index % 10 < 7 else f"Routine archive message {index}"
            )
        elif workload == "multi":
            if index == 0:
                content = "session_alpha session_beta exact target"
            elif index % 3 == 1:
                content = f"session_alpha partial {index}"
            elif index % 3 == 2:
                content = f"session_beta partial {index}"
            else:
                content = f"Routine archive message {index}"
        else:
            content = f"Routine archive message {index}"
        messages.append((message_id, session["id"], "user", content, now))
        terms.extend((message_id, token) for token in _memory_search_tokens(content))
    with store.connect() as con:
        con.executemany(
            "INSERT INTO session_messages(id,session_id,role,content,created_at) "
            "VALUES(?,?,?,?,?)",
            messages,
        )
        con.executemany(
            "INSERT INTO session_message_search_terms(message_id,term) VALUES(?,?)",
            terms,
        )
    return target_id if workload in {"rare", "multi"} else None


def verify(results: list[dict], workload: str, count: int, target_id: str | None) -> bool:
    if workload == "rare":
        return len(results) == 1 and results[0]["id"] == target_id
    if workload == "multi":
        return bool(results) and results[0]["id"] == target_id
    if workload == "common":
        expected = sum(1 for index in range(count) if index % 10 < 7)
        return len(results) == min(5, expected) and all(
            "common" in _memory_search_tokens(item["content"]) for item in results
        )
    return results == []


def benchmark(count: int, iterations: int, workload: str) -> dict[str, object]:
    queries = {
        "rare": "CROWS_SESSION_NEEDLE",
        "common": "common",
        "multi": "session_alpha session_beta",
        "no_match": "CROWS_SESSION_ABSENT",
    }
    query = queries[workload]
    with tempfile.TemporaryDirectory(prefix="crows-session-search-") as directory:
        store = TaskStore(Path(directory) / "benchmark.sqlite3")
        target_id = seed_messages(store, count, workload)
        query_terms = sorted(_memory_search_tokens(query))
        with store.connect() as con:
            con.execute(
                "CREATE TEMP TABLE query_session_message_terms "
                "(term TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            con.executemany(
                "INSERT INTO query_session_message_terms(term) VALUES(?)",
                [(term,) for term in query_terms],
            )
            plan = [
                row["detail"] for row in con.execute(
                    "EXPLAIN QUERY PLAN SELECT indexed.message_id, COUNT(*) AS score "
                    "FROM query_session_message_terms AS query "
                    "CROSS JOIN session_message_search_terms AS indexed "
                    "INDEXED BY idx_session_message_search_terms_term "
                    "WHERE indexed.term=query.term "
                    "GROUP BY indexed.message_id"
                )
            ]
        index_used = any(
            "SEARCH indexed USING COVERING INDEX idx_session_message_search_terms_term" in line
            for line in plan
        )
        if not index_used:
            raise RuntimeError("Cross-session search query plan did not use the term index: " + "; ".join(plan))
        for _ in range(3):
            if not verify(store.search_session_messages(query, limit=5), workload, count, target_id):
                raise RuntimeError(f"Result verification failed: {workload} at {count} messages")
        samples = []
        for _ in range(iterations):
            started = time.perf_counter()
            results = store.search_session_messages(query, limit=5)
            samples.append((time.perf_counter() - started) * 1000)
            if not verify(results, workload, count, target_id):
                raise RuntimeError(f"Search result changed during benchmark: {workload} at {count}")
        return {
            "workload": workload,
            "message_count": count,
            "iterations": iterations,
            "result_count": len(results),
            "p50_ms": round(statistics.median(samples), 3),
            "p95_ms": round(percentile(samples, 0.95), 3),
            "result_verified": True,
            "term_index_used": index_used,
            "query_plan": plan,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000])
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()
    if args.iterations < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and iterations must be positive")
    report = {
        "benchmark": "crows_cross_session_message_search",
        "results": [
            benchmark(size, args.iterations, workload)
            for size in args.sizes
            for workload in ("rare", "common", "multi", "no_match")
        ],
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
