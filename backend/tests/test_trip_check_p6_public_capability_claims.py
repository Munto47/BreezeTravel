from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_CAPABILITY_FILES = (
    "frontend/src/app/layout.tsx",
    "frontend/src/app/page.tsx",
    "frontend/src/app/login/page.tsx",
    "frontend/src/app/about/page.tsx",
)
FROZEN_ASSET_CLAIMS = (
    "AI 智能旅行协同规划",
    "AI 旅行协同规划",
    "LangGraph 多 Agent",
    "DeepSeek + Qwen2.5 LoRA",
    "游记 RAG + 实时 POI",
    "Yjs CRDT 500ms 同步",
    "K-Means 聚类 + TSP",
    "深度推荐：含游记知识库",
    "实时协同选点",
)


def _public_capability_text() -> str:
    return "\n".join(
        (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in PUBLIC_CAPABILITY_FILES
    )


def test_public_capability_surface_describes_only_trip_check_scope():
    text = _public_capability_text()

    assert "BreezeTravel — 行程查" in text
    assert "北京、上海或杭州" in text
    assert "2～5 人" in text
    assert "UNKNOWN 不伪装成通过" in text
    assert "自动验证不等于真人证据" in text
    for frozen_claim in FROZEN_ASSET_CLAIMS:
        assert frozen_claim not in text


def test_primary_home_entry_does_not_expose_frozen_planner_routes():
    home = (REPO_ROOT / "frontend/src/app/page.tsx").read_text(encoding="utf-8")

    assert "创建并导入行程" in home
    assert "导入行程并核验" in home
    assert "setEntryMode" not in home
    assert "'/templates" not in home
    assert "`/room/${createdRoomInfo.roomId}" not in home
