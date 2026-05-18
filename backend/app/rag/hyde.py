"""
HyDE（Hypothetical Document Embeddings）查询扩展

原理
----
标准 RAG 直接对用户查询做 embedding，但查询往往是疑问句或短关键词，
与游记这类长文档的语义分布存在偏差（query-document mismatch）。

HyDE 的做法：
  1. 用 LLM 根据查询生成一段"假设性游记片段"（100-150 字）
  2. 对假设文档而非原始查询做 embedding
  3. 假设文档的语义空间更接近真实游记，可以显著提升 recall

参考：Gao et al. 2022 "Precise Zero-Shot Dense Retrieval without Relevance Labels"
  https://arxiv.org/abs/2212.10496

使用方式
--------
  from app.rag.hyde import generate_hypothetical_doc
  hyp_text = await generate_hypothetical_doc(query="成都有哪些好吃的火锅", city="成都")
  embedding  = await embed_text(hyp_text)
"""

from openai import AsyncOpenAI

from app.config import settings

_client: AsyncOpenAI | None = None

# ── Prompt ────────────────────────────────────────────────────────────────────
_HYDE_SYSTEM = (
    "你是一位经验丰富的旅行作家，擅长用生动的第一人称写游记。"
    "请根据用户的旅行查询，写一段真实感强的游记片段，"
    "包含具体地点体验、实用避坑建议、推荐理由等细节。"
    "直接输出游记内容，不要有任何前缀或解释。"
)

_HYDE_USER = """查询：{query}
目的地：{city}

请写一段 100-150 字的第一人称游记片段（覆盖查询所问的内容）："""


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.effective_llm_api_key,
            base_url=settings.effective_llm_api_url,
        )
    return _client


async def generate_hypothetical_doc(query: str, city: str = "") -> str:
    """
    生成假设性游记文档（HyDE）

    Args:
        query : 用户的原始查询或改写后的查询
        city  : 目的地城市（用于给 LLM 提供上下文）

    Returns:
        假设性游记文本；若生成失败则回退返回原始查询
    """
    if not settings.hyde_enabled:
        return query

    has_key = bool(settings.effective_llm_api_key)
    if not has_key:
        return query

    try:
        client = _get_client()
        resp = await client.chat.completions.create(
            model=settings.hyde_model,
            messages=[
                {"role": "system", "content": _HYDE_SYSTEM},
                {
                    "role": "user",
                    "content": _HYDE_USER.format(
                        query=query,
                        city=city or "目的地",
                    ),
                },
            ],
            max_tokens=200,
            temperature=0.7,
        )
        hyp_doc = resp.choices[0].message.content.strip()
        print(f"[HyDE] 假设文档生成成功（{len(hyp_doc)} 字）：{hyp_doc[:40]}...")
        return hyp_doc

    except Exception as exc:
        print(f"[HyDE] 假设文档生成失败，回退到原始查询：{exc}")
        return query
