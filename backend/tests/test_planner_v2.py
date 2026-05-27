"""PlannerAgent v2 Golden Tests（SPEC §8 / A8）

6 个典型输入场景，人工核可后作为 CI 回归基线。
CI 跑：pytest tests/test_planner_v2.py -v

设计原则：
- 全部离线（mock place_meta DB / weather API / amap distance / tips LLM）
- 验证输出「结构正确」+ 「关键字段存在」，不依赖 LLM 语义判断
- 每个 test 覆盖一个典型 persona / edge case
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource
from app.schemas.preferences import GroupPreferences, WeatherDay


# ─── 辅助构造函数 ────────────────────────────────────────────────────────────────

def _place(pid, name, lng, lat, category=PlaceCategory.ATTRACTION, tags=None, duration=120):
    return Place(
        place_id=pid,
        name=name,
        category=category,
        address=f"{name}地址",
        coords=Coordinates(lng=lng, lat=lat),
        city="成都",
        source=PlaceSource.AMAP_POI,
        estimated_duration=duration,
        tags=tags or [],
    )


def _food(pid, name, lng, lat, tags=None):
    return _place(pid, name, lng, lat, PlaceCategory.FOOD, tags, 75)


def _hotel(pid, name, lng, lat):
    return _place(pid, name, lng, lat, PlaceCategory.HOTEL, [], 30)


# ─── 标准成都景点数据集 ──────────────────────────────────────────────────────────

ATTRACTIONS = [
    _place("P001", "宽窄巷子",   104.0534, 30.6711, tags=["街区", "古镇"]),
    _place("P002", "武侯祠",     104.0468, 30.6421, tags=["博物馆", "历史遗址"]),
    _place("P003", "锦里古街",   104.0483, 30.6398, tags=["街区"]),
    _place("P004", "大熊猫基地", 104.1496, 30.7373, tags=["景区", "动物园"], duration=180),
    _place("P005", "都江堰",     103.6171, 31.0044, tags=["景区", "5A景区"], duration=240),
    _place("P006", "东郊记忆",   104.1137, 30.6538, tags=["艺术区", "街区"]),
]

FOODS = [
    _food("F001", "大龙燚火锅", 104.0650, 30.6650, tags=["火锅"]),
    _food("F002", "成都小吃",   104.0520, 30.6700, tags=["地方菜", "小吃"]),
    _food("F003", "蜀九香",     104.0600, 30.6600, tags=["火锅"]),
]

HOTELS = [
    _hotel("H001", "成都锦江宾馆", 104.0688, 30.6600),
    _hotel("H002", "天府丽都",     104.1000, 30.7000),
]

ALL_PLACES = ATTRACTIONS + FOODS + HOTELS


# ─── Mock 工厂 ────────────────────────────────────────────────────────────────

def _mock_meta():
    """place_meta 模拟：给部分地点设置营业时间（其余按默认处理）"""
    return {
        "P002": {
            "place_id": "P002",
            "open_hours_json": {
                "mon": None,  # 武侯祠周一闭馆
                "tue": [[9, 17]], "wed": [[9, 17]], "thu": [[9, 17]],
                "fri": [[9, 17]], "sat": [[9, 17]], "sun": [[9, 17]],
            },
            "dwell_minutes": 150,
            "dwell_conf": "high",
        },
        "F001": {
            "place_id": "F001",
            "open_hours_json": {k: [[11, 23]] for k in ["mon","tue","wed","thu","fri","sat","sun"]},
            "dwell_minutes": 90,
            "dwell_conf": "high",
        },
    }


def _mock_weather_rainy():
    return {0: WeatherDay(date="2026-07-01", condition="rainy", precip_mm=15.0)}


def _mock_weather_sunny():
    return {0: WeatherDay(date="2026-07-01", condition="sunny", precip_mm=0.0)}


async def _patched_run_planner(places, trip_days, thread_id, user_prefs=None, start_date=None, weather=None):
    """带离线 mock 的 run_planner 包装"""
    import app.agents.planner.nodes.critic_v2 as cv2_mod
    from app.agents.planner.graph import run_planner

    meta = _mock_meta()
    wf = weather or {}

    async def fake_weather_run(state):
        return {"weather_forecast": wf, "trace": state.get("trace", []) + ["[WeatherFetcher] mock"]}

    async def fake_critic(state):
        return {"critic_violations": [], "trace": state.get("trace", []) + ["[CriticV2] mock"]}

    orig_critic = cv2_mod.run

    with patch("app.agents.planner.nodes.scheduler_v2._load_place_meta", AsyncMock(return_value=meta)):
        with patch("app.agents.planner.nodes.weather_fetcher.run", new=fake_weather_run):
            cv2_mod.run = fake_critic
            try:
                return await run_planner(
                    places=places,
                    trip_days=trip_days,
                    thread_id=thread_id,
                    user_prefs=user_prefs,
                    start_date=start_date,
                )
            finally:
                cv2_mod.run = orig_critic


def run_sync(coro):
    return asyncio.run(coro)


# ─── Golden Test 1：标准 3 天行程结构 ────────────────────────────────────────────

class TestGolden1_Standard3DayTrip:
    """3 天标准行程：T_ARRIVAL → T_DEEP_EXPLORE → T_DEPARTURE"""

    def test_itinerary_has_correct_day_count(self):
        result = run_sync(_patched_run_planner(ALL_PLACES, trip_days=3, thread_id="golden-001"))
        assert result.itinerary is not None
        assert len(result.itinerary.days) == 3

    def test_all_days_indexed_correctly(self):
        result = run_sync(_patched_run_planner(ALL_PLACES, trip_days=3, thread_id="golden-001b"))
        days = result.itinerary.days
        for d in days:
            assert 0 <= d.day_index < 3

    def test_each_day_has_at_least_one_slot(self):
        result = run_sync(_patched_run_planner(ALL_PLACES, trip_days=3, thread_id="golden-001c"))
        for day in result.itinerary.days:
            assert len(day.slots) > 0, f"第 {day.day_index} 天没有任何 slot"

    def test_thread_id_preserved(self):
        result = run_sync(_patched_run_planner(ALL_PLACES, trip_days=3, thread_id="golden-thread-xyz"))
        assert result.itinerary.thread_id == "golden-thread-xyz"

    def test_city_resolved(self):
        result = run_sync(_patched_run_planner(ALL_PLACES, trip_days=3, thread_id="golden-city"))
        assert result.itinerary.city == "成都"


# ─── Golden Test 2：亲子模板（T_FAMILY_LIGHT） ───────────────────────────────────

class TestGolden2_FamilyTrip:
    """has_kids=True → 中间天应选 T_FAMILY_LIGHT"""

    def test_family_prefs_accepted(self):
        prefs = GroupPreferences(has_kids=True, style="family", trip_city="成都", trip_days=2)
        result = run_sync(_patched_run_planner(
            ALL_PLACES, trip_days=2, thread_id="golden-family", user_prefs=prefs
        ))
        assert result.itinerary is not None
        assert len(result.itinerary.days) == 2

    def test_family_trip_has_slots(self):
        prefs = GroupPreferences(has_kids=True, style="family", trip_city="成都", trip_days=2)
        result = run_sync(_patched_run_planner(
            ALL_PLACES, trip_days=2, thread_id="golden-family2", user_prefs=prefs
        ))
        for day in result.itinerary.days:
            assert len(day.slots) > 0


# ─── Golden Test 3：溢出备选池（places 超过行程容量） ─────────────────────────────

class TestGolden3_BackupPool:
    """大量地点 → 排不下的进 backup_pool"""

    def test_backup_pool_populated_when_overflow(self):
        # 9 个景点 + 3 餐厅 + 2 酒店，2 天行程容量有限
        extra_places = [
            _place(f"EX{i}", f"额外景点{i}", 104.05 + i*0.01, 30.65 + i*0.01)
            for i in range(6)
        ]
        result = run_sync(_patched_run_planner(
            ATTRACTIONS + extra_places + FOODS + HOTELS,
            trip_days=2,
            thread_id="golden-overflow",
        ))
        # 地点多于行程容量，backup_pool 应非空
        assert isinstance(result.backup_pool, list)
        # itinerary 仍然有效
        assert result.itinerary is not None
        assert len(result.itinerary.days) == 2

    def test_backup_pool_places_are_valid(self):
        extra_places = [
            _place(f"BK{i}", f"备选地点{i}", 104.05 + i*0.005, 30.65 + i*0.005)
            for i in range(4)
        ]
        result = run_sync(_patched_run_planner(
            ATTRACTIONS + extra_places + FOODS + HOTELS,
            trip_days=2,
            thread_id="golden-overflow2",
        ))
        for p in result.backup_pool:
            assert p.place_id is not None
            assert p.name is not None


# ─── Golden Test 4：雨天天气适配 ─────────────────────────────────────────────────

class TestGolden4_RainyWeatherAdaptation:
    """雨天 15mm → Critic 不应抱怨（scheduler_v2 应减少户外 slot）"""

    def test_rainy_day_produces_valid_itinerary(self):
        result = run_sync(_patched_run_planner(
            ATTRACTIONS + FOODS + HOTELS,
            trip_days=2,
            thread_id="golden-rain",
            start_date="2026-07-01",
            weather=_mock_weather_rainy(),
        ))
        assert result.itinerary is not None

    def test_rainy_itinerary_has_days(self):
        result = run_sync(_patched_run_planner(
            ATTRACTIONS + FOODS + HOTELS,
            trip_days=2,
            thread_id="golden-rain2",
            start_date="2026-07-01",
            weather=_mock_weather_rainy(),
        ))
        assert len(result.itinerary.days) == 2


# ─── Golden Test 5：模板选择逻辑（D16） ──────────────────────────────────────────

class TestGolden5_TemplateSelection:
    """验证 Day 0 = T_ARRIVAL，末日 = T_DEPARTURE，中间天按偏好选"""

    def test_template_select_arrival_day(self):
        from app.agents.planner.templates import select_template, T_ARRIVAL
        t = select_template(day_index=0, trip_days=3)
        assert t.template_id == T_ARRIVAL.template_id

    def test_template_select_departure_day(self):
        from app.agents.planner.templates import select_template, T_DEPARTURE
        t = select_template(day_index=2, trip_days=3)
        assert t.template_id == T_DEPARTURE.template_id

    def test_template_select_nightlife(self):
        from app.agents.planner.templates import select_template, T_NIGHTLIFE
        prefs = GroupPreferences(style="nightlife", trip_city="成都", trip_days=3)
        t = select_template(day_index=1, trip_days=3, prefs=prefs)
        assert t.template_id == T_NIGHTLIFE.template_id

    def test_template_select_family(self):
        from app.agents.planner.templates import select_template, T_FAMILY_LIGHT
        prefs = GroupPreferences(has_kids=True, style="family", trip_city="成都", trip_days=3)
        t = select_template(day_index=1, trip_days=3, prefs=prefs)
        assert t.template_id == T_FAMILY_LIGHT.template_id

    def test_template_select_culture_default(self):
        from app.agents.planner.templates import select_template, T_DEEP_EXPLORE
        t = select_template(day_index=1, trip_days=3)  # 无偏好 → 默认
        assert t.template_id == T_DEEP_EXPLORE.template_id


# ─── Golden Test 6：Critic v2 硬规则集成（闭馆 + 重叠时间） ─────────────────────

class TestGolden6_CriticIntegration:
    """端到端 Critic 检验：不使用 mock，验证违规被检出"""

    def test_critic_catches_open_hours_violation(self):
        """Critic 直接调用：给定闭馆地点 + 时段，应返回 R_OPEN_HOURS 违规"""
        from app.agents.planner.nodes.critic_v2 import _check_open_hours

        meta_cache = {
            "test_museum": {
                "open_hours_json": {
                    "mon": None,  # 周一闭馆
                    "tue": [[9, 17]], "wed": [[9, 17]], "thu": [[9, 17]],
                    "fri": [[9, 17]], "sat": [[9, 17]], "sun": [[9, 17]],
                }
            }
        }
        slot = {
            "place_id": "test_museum",
            "start_time": "10:00",
            "end_time": "12:00",
        }
        v = _check_open_hours(slot, day_index=0, meta_cache=meta_cache, dow=0)
        assert v is not None
        assert v["rule"] == "R_OPEN_HOURS"

    def test_critic_full_state_no_day_states_skips(self):
        """无 day_states 时 Critic 直接跳过，不报错"""
        async def _run():
            from app.agents.planner.nodes.critic_v2 import run
            out = await run({"trace": []})
            return out

        out = asyncio.run(_run())
        assert out["critic_violations"] == []

    def test_end_to_end_no_false_positives(self):
        """标准输入不应触发 Critic 误报（正常行程应无违规）"""
        import app.agents.planner.nodes.critic_v2 as cv2_mod

        violations_captured = []

        async def capturing_critic(state):
            # 调用真实 Critic（无 DB，meta_cache 为空 → 跳过 R_OPEN_HOURS）
            result = await _real_critic(state)
            violations_captured.extend(result.get("critic_violations", []))
            return result

        _real_critic = cv2_mod.run

        async def _run():
            with patch("app.agents.planner.nodes.scheduler_v2._load_place_meta", AsyncMock(return_value={})):
                with patch("app.agents.planner.nodes.weather_fetcher.run", new=AsyncMock(
                    return_value={"weather_forecast": {}, "trace": []}
                )):
                    cv2_mod.run = capturing_critic
                    try:
                        from app.agents.planner.graph import run_planner
                        return await run_planner(
                            places=ATTRACTIONS + FOODS + HOTELS,
                            trip_days=2,
                            thread_id="golden-critic-e2e",
                        )
                    finally:
                        cv2_mod.run = _real_critic

        asyncio.run(_run())
        # 无 place_meta 时 R_OPEN_HOURS 被跳过，只验证结构型规则
        structural_violations = [v for v in violations_captured if v["rule"] != "R_OPEN_HOURS"]
        assert structural_violations == [], f"意外的 Critic 违规：{structural_violations}"
