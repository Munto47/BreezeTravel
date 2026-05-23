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

# 已知地标名（精确匹配时，查询本身已足够具体，HyDE 引入语义漂移风险大于收益）
_KNOWN_LANDMARKS = {
    # 成都
    "宽窄巷子", "锦里", "都江堰", "九寨沟", "熊猫基地", "青城山", "太古里", "春熙路",
    # 北京
    "故宫", "长城", "颐和园", "天坛", "南锣鼓巷", "798", "三里屯", "圆明园",
    # 上海
    "外滩", "豫园", "田子坊", "新天地", "朱家角", "陆家嘴", "南京路",
    # 厦门
    "鼓浪屿", "曾厝垵", "南普陀", "集美", "中山路",
    # 广州
    "陈家祠", "越秀公园", "广州塔", "沙面", "北京路",
    # 深圳
    "大鹏半岛", "东门老街", "华强北", "世界之窗", "深圳湾",
    # 杭州
    "西湖", "灵隐寺", "乌镇", "雷峰塔", "西溪湿地", "南宋御街", "河坊街",
}


def _should_skip_hyde(query: str) -> bool:
    """
    判断是否跳过 HyDE 生成。

    跳过条件（任一满足）：
    1. 查询长度 < 12 字：通常是直接输入景点名，语义已足够精确
    2. 查询去除城市名/标点后恰好是一个已知地标名：直接查更准

    保留 HyDE 的场景：长句描述性查询（"成都适合亲子的自然景区有哪些"）。
    """
    stripped = query.strip()
    if len(stripped) < 12:
        return True
    for landmark in _KNOWN_LANDMARKS:
        if stripped == landmark or stripped.endswith(landmark) and len(stripped) - len(landmark) <= 4:
            return True
    return False


_client: AsyncOpenAI | None = None

# ── Intent-aware 系统 Prompt ──────────────────────────────────────────────────
# 不同意图类型的假设文档侧重点不同：
#   hotel  → 必须含具体酒店名、价格区间、位置描述
#   food   → 必须含具体餐厅名、招牌菜、价格、排队情况
#   tips   → 必须含具体避坑点、时间建议、注意事项
#   transport → 必须含具体线路、时长、票价
#   scenic/默认 → 体验描述 + 推荐理由
_HYDE_SYSTEM_DEFAULT = (
    "你是一位经验丰富的旅行作家，擅长用生动的第一人称写游记。"
    "请根据用户的旅行查询，写一段真实感强的游记片段，"
    "包含具体地点体验、实用避坑建议、推荐理由等细节。"
    "直接输出游记内容，不要有任何前缀或解释。"
)

_HYDE_SYSTEM_HOTEL = (
    "你是一位经验丰富的旅行作家，专注于住宿攻略。"
    "请写一段第一人称的住宿推荐游记片段，必须包含：具体酒店/民宿名称、"
    "价格区间（元/晚）、地理位置优势（距哪个景点/地铁站多远）、"
    "房间体验、早餐评价等实用信息。直接输出游记内容，不加任何前缀。"
)

_HYDE_SYSTEM_FOOD = (
    "你是一位美食达人旅行作家，专注于餐饮攻略。"
    "请写一段第一人称的美食游记片段，必须包含：具体餐厅/小吃摊名称、"
    "招牌菜品名、人均价格、营业时间/排队情况、口味特点等实用细节。"
    "直接输出游记内容，不加任何前缀。"
)

_HYDE_SYSTEM_TIPS = (
    "你是一位经验丰富的旅行博主，专注于避坑攻略。"
    "请写一段第一人称的旅行避坑游记，必须包含：具体注意事项（预约/排队/天气）、"
    "常见坑点、最佳游览时间/季节建议、省钱技巧等实用信息。"
    "直接输出游记内容，不加任何前缀。"
)

_HYDE_SYSTEM_TRANSPORT = (
    "你是一位精通城市交通的旅行作家。"
    "请写一段第一人称的交通攻略游记，必须包含：具体交通方式（地铁线路号/高铁/打车）、"
    "起终站名称、所需时长、票价费用、换乘注意事项等精确信息。"
    "直接输出游记内容，不加任何前缀。"
)

# intent → system prompt 映射
_INTENT_SYSTEM_MAP: dict[str, str] = {
    "hotel":     _HYDE_SYSTEM_HOTEL,
    "food":      _HYDE_SYSTEM_FOOD,
    "tips":      _HYDE_SYSTEM_TIPS,
    "transport": _HYDE_SYSTEM_TRANSPORT,
    "scenic":    _HYDE_SYSTEM_DEFAULT,
}

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


async def generate_hypothetical_doc(
    query: str,
    city: str = "",
    intent: str = "",
) -> str:
    """
    生成假设性游记文档（HyDE）

    Args:
        query  : 用户的原始查询或改写后的查询
        city   : 目的地城市（用于给 LLM 提供上下文）
        intent : 意图类型（hotel/food/tips/transport/scenic），
                 用于选择意图感知的 System Prompt，提升特定类型的 Context Recall。
                 不传或传 "" 时使用默认 prompt。

    Returns:
        假设性游记文本；若生成失败则回退返回原始查询
    """
    if not settings.hyde_enabled:
        return query

    if _should_skip_hyde(query):
        print(f"[HyDE] 查询简短/精确地名，跳过假设文档生成：{query!r}")
        return query

    has_key = bool(settings.effective_llm_api_key)
    if not has_key:
        return query

    # 选择意图感知的 system prompt
    system_prompt = _INTENT_SYSTEM_MAP.get(intent, _HYDE_SYSTEM_DEFAULT)
    intent_label = intent or "default"

    try:
        client = _get_client()
        resp = await client.chat.completions.create(
            model=settings.hyde_model,
            messages=[
                {"role": "system", "content": system_prompt},
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
        print(f"[HyDE] 假设文档生成成功（intent={intent_label}，{len(hyp_doc)} 字）：{hyp_doc[:40]}...")
        return hyp_doc

    except Exception as exc:
        print(f"[HyDE] 假设文档生成失败，回退到原始查询：{exc}")
        return query
