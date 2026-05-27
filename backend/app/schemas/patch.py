"""行程编辑 Patch Schema（SPEC §4.3 / C3）

ItineraryPatch — EditorAgent 或 Rule Fast Path 输出的结构化变更指令：
  op           操作类型
  day_index    目标天（0-based）
  slot_index   目标槽位（None = 新增到末尾）
  target_place_id  被替换/删除的 place_id
  new_place_id     新地点 ID（replace_place / add_place）
  new_template_id  rebuild_day 时指定新模板
  rationale    为什么做这个变更（LLM 解释 / 规则说明）
"""

from typing import Literal, Optional
from pydantic import BaseModel


class ItineraryPatch(BaseModel):
    op: Literal["replace_place", "add_place", "remove_place", "swap_days", "rebuild_day"]
    day_index: int
    slot_index: Optional[int] = None
    target_place_id: Optional[str] = None    # 被操作的 place_id
    new_place_id: Optional[str] = None       # 替换/新增的 place_id
    new_place_query: Optional[str] = None    # 用于 RAG 检索新候选（EditorAgent 用）
    new_template_id: Optional[str] = None   # rebuild_day 时指定模板
    rationale: str = ""
    affects_global: bool = False             # True 时需要全图重跑（质心变化）


class PatchResult(BaseModel):
    """Patch 应用结果"""
    success: bool
    message: str
    patch: ItineraryPatch
    violations: list[dict] = []              # Critic 检出的违规
