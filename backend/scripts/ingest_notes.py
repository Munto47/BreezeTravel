"""
游记入库脚本（Advanced RAG 版本）

用法：
  # 容器内（推荐）
  docker compose exec backend python -m scripts.ingest_notes

  # 本地（需先设置环境变量）
  cd backend && python -m scripts.ingest_notes

  # 只重建 BM25 索引（不重新生成游记）
  cd backend && python -m scripts.ingest_notes --rebuild-tokens

流程：
  1. 调用 LLM 批量生成游记（成都/北京/上海/厦门/广州/深圳/杭州各 50 篇，共 350 篇）
     主 LLM：DeepSeek API（deepseek-chat）；备用：OpenAI gpt-4o-mini
  2. Entity Linking：地点名 → 高德 POI ID（AMAP_MOCK=true 时跳过）
  3. 文本分块（chunk_size=500, overlap=50）
     + jieba 中文分词 → content_tokens（供 BM25 使用）
     + text-embedding-3-small Embedding（供 Dense 检索使用）
  4. 批量写入 pgvector（travel_notes + travel_notes_chunks 表）
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import asyncpg
import aiohttp
import jieba
from openai import AsyncOpenAI

# 让脚本能 import app 模块
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings

# ===== 配置 =====
CITIES = {
    "成都": "cd",
    "北京": "bj",
    "上海": "sh",
    "厦门": "xm",
    "广州": "gz",
    "深圳": "sz",
    "杭州": "hz",
}
NOTES_PER_CITY = 50
PERSONAS = [
    "亲子游", "情侣旅行", "带老人出行", "背包客独游", "闺蜜旅行",
    "商务差旅", "学生党穷游", "摄影爱好者", "美食达人", "历史文化爱好者",
]
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
EMBEDDING_BATCH = 50   # 每批 Embedding 数量


# ===== 工具函数 =====

def _tokenize_chinese(text: str) -> str:
    """
    jieba 精确模式分词，返回空格分隔字符串。
    示例："成都有哪些好吃的" → "成都 有 哪些 好吃 的"
    用于 PostgreSQL tsvector BM25 检索。
    """
    tokens = jieba.cut(text, cut_all=False)
    return " ".join(t for t in tokens if t.strip())


def _make_llm_client() -> AsyncOpenAI:
    """
    构建 LLM 客户端：优先 DeepSeek，回退 OpenAI。
    游记生成使用主 LLM（deepseek-chat），文风更自然。
    """
    if settings.deepseek_api_key:
        print(f"[LLM] 使用 DeepSeek API（{settings.deepseek_api_url}）")
        return AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_api_url,
        )
    print(f"[LLM] 使用 OpenAI 兼容接口（{settings.openai_api_url}）")
    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_api_url,
    )


def _make_embedding_client() -> AsyncOpenAI:
    """构建 Embedding 客户端（独立于主 LLM 配置）"""
    return AsyncOpenAI(
        api_key=settings.effective_embedding_api_key,
        base_url=settings.effective_embedding_api_url,
    )


# ===== Step 1：LLM 生成游记 =====

# 各城市的特色引导关键词，帮助 LLM 生成有辨识度的本地化内容
_CITY_HIGHLIGHTS = {
    "成都": "宽窄巷子、锦里、都江堰、九寨沟、火锅、串串香、熊猫基地、青城山、太古里、玉林路",
    "北京": "故宫、长城、颐和园、南锣鼓巷、798艺术区、胡同、烤鸭、簋街、三里屯、天坛",
    "上海": "外滩、豫园、田子坊、新天地、朱家角、南京路、陆家嘴、小笼包、生煎、夜生活",
    "厦门": "鼓浪屿、曾厝垵、南普陀、集美、中山路、沙茶面、海蛎煎、土笋冻、海上花园、闽南文化",
    "广州": "陈家祠、越秀公园、广州塔、沙面、北京路、早茶、肠粉、烧腊、老字号、岭南建筑",
    "深圳": "大鹏半岛、东门老街、华强北、世界之窗、沙井蚝、罗湖口岸、深圳湾、科技园、西涌、户外徒步",
    "杭州": "西湖、灵隐寺、乌镇、雷峰塔、龙井茶、东坡肉、西溪湿地、南宋御街、河坊街、钱塘江",
}

GENERATE_PROMPT = """请生成一篇真实感强的{city}{days}日{persona}游记，严格要求：
1. 包含 5-8 个具体的{city}景点/餐厅/街道名称（优先从以下特色地点中选取：{city_highlights}）
2. 包含至少 4 条具体避坑经验（如"xx景点北门排队少，建议走北门入场"）
3. 包含至少 2 条交通建议（地铁/公交线路、打车费用参考）
4. 字数 800-1000 字，第一人称叙述，口语化风格，每篇内容差异化

必须返回合法 JSON，格式（不要有其他文字）：
{{"id": "note-{city_en}-{idx:03d}", "title": "标题", "city": "{city}", "content": "游记正文...", "tags": ["标签1","标签2"], "places_mentioned": ["地点1","地点2","..."]}}"""


async def generate_one_note(
    client: AsyncOpenAI,
    city: str,
    city_en: str,
    idx: int,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    persona = PERSONAS[idx % len(PERSONAS)]
    days = [2, 3, 3, 4, 5][idx % 5]
    city_highlights = _CITY_HIGHLIGHTS.get(city, city)
    prompt = GENERATE_PROMPT.format(
        city=city, days=days, persona=persona,
        city_en=city_en, idx=idx,
        city_highlights=city_highlights,
    )
    # 根据可用 Key 选择模型
    model = settings.llm_model_synthesizer  # deepseek-chat 或 gpt-4o-mini

    async with semaphore:
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
                temperature=0.8,
            )
            raw = resp.choices[0].message.content.strip()
            # 提取 JSON（兼容 LLM 输出前后有多余文字的情况）
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                note = json.loads(m.group())
                print(f"  ✓ 生成：{note.get('title', '?')}")
                return note
        except Exception as e:
            print(f"  ✗ 生成失败（{city} #{idx}）：{e}")
    return None


async def generate_notes(client: AsyncOpenAI) -> list[dict]:
    print("\n[Step 1] 生成游记...")
    semaphore = asyncio.Semaphore(5)   # 控制并发避免限速
    tasks = []
    for city, city_en in CITIES.items():
        for idx in range(NOTES_PER_CITY):
            tasks.append(generate_one_note(client, city, city_en, idx, semaphore))

    results = await asyncio.gather(*tasks)
    notes = [n for n in results if n is not None]
    print(f"[Step 1] 完成：{len(notes)}/{len(tasks)} 篇游记生成成功")
    return notes


# ===== Step 2：Entity Linking =====

async def entity_linking(notes: list[dict], session: aiohttp.ClientSession) -> list[dict]:
    """将 places_mentioned 中的地点名映射到高德 POI ID"""
    if settings.amap_mock:
        print("[Step 2] AMAP_MOCK=true，跳过 Entity Linking（place_ids 留空）")
        for note in notes:
            note["place_id_map"] = {}
        return notes

    print("\n[Step 2] Entity Linking（高德 POI 搜索）...")
    for note in notes:
        place_id_map = {}
        for place_name in note.get("places_mentioned", []):
            try:
                async with session.get(
                    "https://restapi.amap.com/v3/place/text",
                    params={
                        "key": settings.amap_api_key,
                        "keywords": place_name,
                        "city": note["city"],
                        "output": "json",
                        "offset": 1,
                    },
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "1" and data.get("pois"):
                        place_id_map[place_name] = data["pois"][0]["id"]
            except Exception:
                pass
            await asyncio.sleep(0.1)  # 高德 QPS 限制
        note["place_id_map"] = place_id_map
    print("[Step 2] Entity Linking 完成")
    return notes


# ===== Step 3：分块 =====

def split_into_chunks(text: str) -> list[dict]:
    """按段落优先切分，超长段落再按字数切分"""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) <= CHUNK_SIZE:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
            # 超长单段落按字数切
            if len(para) > CHUNK_SIZE:
                for start in range(0, len(para), CHUNK_SIZE - CHUNK_OVERLAP):
                    chunks.append(para[start : start + CHUNK_SIZE])
            else:
                current = para
    if current:
        chunks.append(current)
    return [{"text": c} for c in chunks if c]


# ===== Step 4：Embedding + 写入 pgvector =====

async def ingest_to_pgvector(
    notes: list[dict],
    emb_client: AsyncOpenAI,
    pool: asyncpg.Pool,
) -> None:
    print(f"\n[Step 3] jieba 分词 + Embedding + 写入 pgvector（共 {len(notes)} 篇）...")

    # 预先收集所有 chunk（带 note 引用）
    all_items = []
    for note in notes:
        chunks = split_into_chunks(note["content"])
        for idx, chunk in enumerate(chunks):
            content_tokens = _tokenize_chinese(chunk["text"])
            all_items.append({
                "note_id": note["id"],
                "chunk_idx": idx,
                "city": note["city"],
                "text": chunk["text"],
                "content_tokens": content_tokens,       # jieba 分词结果（BM25 用）
                "place_ids": [
                    note["place_id_map"].get(pname, "")
                    for pname in note.get("places_mentioned", [])
                    if note.get("place_id_map", {}).get(pname)
                ],
                "note": note,
            })

    print(f"  分块完成：{len(all_items)} 个 chunk，开始 Embedding...")

    # 按批 Embedding
    embeddings: list[list[float]] = []
    for i in range(0, len(all_items), EMBEDDING_BATCH):
        batch = all_items[i : i + EMBEDDING_BATCH]
        try:
            resp = await emb_client.embeddings.create(
                model=settings.embedding_model,
                input=[item["text"] for item in batch],
            )
            embeddings.extend(e.embedding for e in resp.data)
            print(f"  Embedding 批次 {i // EMBEDDING_BATCH + 1} 完成（{len(batch)} 条）")
        except Exception as e:
            print(f"  ✗ Embedding 失败（批次 {i // EMBEDDING_BATCH + 1}）：{e}")
            dim = 1024  # BAAI/bge-m3 维度（SiliconFlow）
            embeddings.extend([[0.0] * dim] * len(batch))

    # 写入数据库
    async with pool.acquire() as conn:
        # 先写 travel_notes 表
        note_ids_written: set[str] = set()
        for item in all_items:
            note = item["note"]
            if note["id"] not in note_ids_written:
                await conn.execute(
                    """INSERT INTO travel_notes (id, title, city, content, tags)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (id) DO NOTHING""",
                    note["id"], note.get("title", ""),
                    note["city"], note["content"],
                    note.get("tags", []),
                )
                note_ids_written.add(note["id"])

        # 再写 travel_notes_chunks 表（含 embedding + content_tokens）
        for item, embedding in zip(all_items, embeddings):
            await conn.execute(
                """INSERT INTO travel_notes_chunks
                   (note_id, chunk_idx, city, content, content_tokens, place_ids, embedding)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                   ON CONFLICT DO NOTHING""",
                item["note_id"], item["chunk_idx"], item["city"],
                item["text"], item["content_tokens"],
                item["place_ids"], str(embedding),  # pgvector::vector 需要字符串格式 "[v1,v2,...]"
            )

    print(f"[Step 3] 写入完成：{len(note_ids_written)} 篇游记，{len(all_items)} 个 chunk")


# ===== 重建 content_tokens（不重新生成游记）=====

async def rebuild_tokens(pool: asyncpg.Pool) -> None:
    """
    对已入库但缺少 content_tokens 的 chunk 重新做 jieba 分词。
    用于升级旧版本数据库时使用：
      python -m scripts.ingest_notes --rebuild-tokens
    """
    print("\n[Rebuild] 重建 content_tokens（jieba 分词）...")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, content FROM travel_notes_chunks WHERE content_tokens = '' OR content_tokens IS NULL"
        )
        print(f"  待处理：{len(rows)} 条 chunk")
        for row in rows:
            tokens = _tokenize_chinese(row["content"])
            await conn.execute(
                "UPDATE travel_notes_chunks SET content_tokens = $1 WHERE id = $2",
                tokens, row["id"],
            )
    print("[Rebuild] content_tokens 重建完成")


# ===== 主流程 =====

async def main(rebuild_tokens_only: bool = False) -> None:
    print("=== 游记入库脚本（Advanced RAG 版）===")

    # 初始化数据库连接池
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        # 仅重建 tokens 模式
        if rebuild_tokens_only:
            await rebuild_tokens(pool)
            return

        # ── 完整入库流程 ─────────────────────────────────────────────
        has_llm_key = bool(settings.deepseek_api_key or settings.openai_api_key)
        if not has_llm_key:
            print("错误：DEEPSEEK_API_KEY 或 OPENAI_API_KEY 未配置，无法生成游记")
            return

        if not settings.effective_embedding_api_key:
            print("错误：未配置 Embedding API Key，无法生成向量")
            return

        llm_client = _make_llm_client()
        emb_client = _make_embedding_client()

        print(f"目标城市：{list(CITIES.keys())} 各 {NOTES_PER_CITY} 篇，"
              f"共 {len(CITIES) * NOTES_PER_CITY} 篇")

        async with aiohttp.ClientSession() as session:
            notes = await generate_notes(llm_client)
            if not notes:
                print("没有生成任何游记，退出")
                return
            notes = await entity_linking(notes, session)
            await ingest_to_pgvector(notes, emb_client, pool)

        # 打印统计
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT city, count(*) AS cnt FROM travel_notes_chunks GROUP BY city ORDER BY city"
            )
            print("\n=== 入库统计 ===")
            for row in rows:
                print(f"  {row['city']}: {row['cnt']} 个 chunk")

    finally:
        await pool.close()

    print("\n=== 入库完成 ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="游记入库脚本")
    parser.add_argument(
        "--rebuild-tokens",
        action="store_true",
        help="仅对已入库的 chunk 重建 content_tokens（jieba 分词），不重新生成游记",
    )
    args = parser.parse_args()
    asyncio.run(main(rebuild_tokens_only=args.rebuild_tokens))
