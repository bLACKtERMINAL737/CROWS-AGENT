"""Shared SQL for indexed memory search.

The normal query intentionally has no fallback scan over all memories. The
recall-intent variant adds explicit-chat memories as a separate candidate
source when the user explicitly asks to recall something.
"""

MEMORY_SEARCH_SQL = """
WITH candidate_scores AS (
    SELECT indexed.memory_id, COUNT(*) AS matched_terms
    FROM query_memory_terms AS matched
    CROSS JOIN memory_search_terms AS indexed
    WHERE indexed.term=matched.term
    GROUP BY indexed.memory_id
)
SELECT m.*, candidate_scores.matched_terms AS search_score
FROM candidate_scores
JOIN memories AS m ON m.id=candidate_scores.memory_id
ORDER BY search_score DESC, m.updated_at DESC, m.id DESC
LIMIT ?
"""

MEMORY_SEARCH_RECALL_SQL = """
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
    WHERE source='explicit_chat_request'
)
SELECT m.*, (
    COALESCE(candidate_scores.matched_terms, 0)
) AS search_score
FROM candidate_ids
JOIN memories AS m ON m.id=candidate_ids.memory_id
LEFT JOIN candidate_scores ON candidate_scores.memory_id=m.id
ORDER BY search_score DESC, m.updated_at DESC, m.id DESC
LIMIT ?
"""
