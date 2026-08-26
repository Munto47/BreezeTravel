from dataclasses import dataclass

from app.config import Settings
from app.rag.hyde import _should_skip_hyde


@dataclass(frozen=True)
class RagExecutionPolicy:
    use_hyde: bool
    use_multi_query: bool
    use_reranker: bool
    reasons: tuple[str, ...]


def select_rag_policy(query: str, intent: str, candidate_count: int, settings: Settings) -> RagExecutionPolicy:
    precise = _should_skip_hyde(query)
    multi_query = bool(
        (settings.multi_query_enabled or intent in {"hotel", "tips", "food"})
        and not precise
    )
    hyde = bool(settings.hyde_enabled and not precise and not multi_query)
    reranker = bool(settings.reranker_enabled and candidate_count >= settings.reranker_min_candidates)
    reasons = []
    reasons.append("precise_query" if precise else "descriptive_query")
    if multi_query:
        reasons.append(f"multi_query_intent:{intent}")
    if reranker:
        reasons.append("candidate_pool_sufficient")
    return RagExecutionPolicy(hyde, multi_query, reranker, tuple(reasons))
