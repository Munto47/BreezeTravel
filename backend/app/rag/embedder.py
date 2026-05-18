"""
Embedding 模块：将文本转化为稠密向量

支持任何 OpenAI 兼容接口：
  - OpenAI text-embedding-3-small（默认）
  - SiliconFlow BAAI/bge-m3
  - 其他兼容服务

配置（.env）：
  EMBEDDING_API_KEY=...   留空则复用 OPENAI_API_KEY
  EMBEDDING_API_URL=...   留空则复用 OPENAI_API_URL
  EMBEDDING_MODEL=text-embedding-3-small
"""

from openai import AsyncOpenAI

from app.config import settings

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.effective_embedding_api_key,
            base_url=settings.effective_embedding_api_url,
        )
    return _client


async def embed_text(text: str) -> list[float]:
    """将单段文本嵌入为向量（用于查询时的实时 embedding）"""
    client = _get_client()
    resp = await client.embeddings.create(
        model=settings.embedding_model,
        input=text,
    )
    return resp.data[0].embedding


async def embed_texts(texts: list[str], batch_size: int = 50) -> list[list[float]]:
    """
    批量嵌入多段文本（用于离线入库）

    Args:
        texts      : 待嵌入文本列表
        batch_size : 每次 API 调用的最大文本数（防止超出 token 限制）

    Returns:
        与输入对应的向量列表；API 失败时该批次以零向量占位
    """
    client = _get_client()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        try:
            resp = await client.embeddings.create(
                model=settings.embedding_model,
                input=batch,
            )
            all_embeddings.extend(e.embedding for e in resp.data)
        except Exception as exc:
            print(f"[Embedder] 批次 {i // batch_size + 1} 失败，使用零向量占位：{exc}")
            # 获取向量维度（1536 for text-embedding-3-small）
            dim = _infer_dim()
            all_embeddings.extend([[0.0] * dim] * len(batch))

    return all_embeddings


def _infer_dim() -> int:
    """根据 embedding 模型名称推断向量维度"""
    model = settings.embedding_model
    if "3-large" in model:
        return 3072
    if "ada-002" in model or "bge-m3" in model:
        return 1536
    return 1536  # 默认 text-embedding-3-small
