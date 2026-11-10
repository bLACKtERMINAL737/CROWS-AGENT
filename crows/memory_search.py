"""Shared SQL for indexed memory search.

Keep the query in one place so diagnostics and benchmarks explain the exact
statement executed by TaskStore.search_memories().
"""

MEMORY_SEARCH_SQL = """
WITH candidate_scores AS (
    SELECT indexed.memory_id, COUNT(*) AS matched_terms
    FROM query_memory_terms AS matched
    CROSS JOIN memory_search_terms AS indexed
    WHERE indexed.term=matched.term
    GROUP BY indexed.memory_id
),
candidate_ids AS (
    SELECT memory_id FROM candidate_scores
    UNION
    SELECT id AS memory_id FROM memories
    WHERE ? AND source='explicit_chat_request'
)
SELECT m.*, (
    COALESCE(candidate_scores.matched_terms, 0)
    + CASE WHEN ? AND m.source='explicit_chat_request' THEN 3 ELSE 0 END
) AS search_score
FROM candidate_ids
JOIN memories AS m ON m.id=candidate_ids.memory_id
LEFT JOIN candidate_scores ON candidate_scores.memory_id=m.id
ORDER BY search_score DESC, m.updated_at DESC, m.id DESC
LIMIT ?
"""
