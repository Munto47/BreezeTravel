"""Phase A 综合测试（SPEC §3 排线升级完整验收）

覆盖：
  - A2: GroupPreferences / WeatherDay schema
  - A3: 鱼骨模板系统 (5 套模板 + select_template 逻辑)
  - A4: SchedulerV2 槽位分配 / 用餐窗口强制 / backup_pool / DayPlannerState
  - A5: WeatherFetcherNode（离线降级路径）
  - A6: Critic v2 全图 mocked run
  - 图编译 / 拓扑 / 节点名称验证（v2 节点替换 v1）
  - run_planner 返回 PlannerResult（itinerary + backup_pool + critic_violations）

全部离线运行，不需要 DB / 外部 API。
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.place import Place, Coordinates, PlaceCategory, PlaceSource
from app.schemas.preferences import GroupPreferences, WeatherDay


# ─── 共用 fixture ──────────────────────────────────────────────────────────────

def make_place(pid, name, lng, lat, category=PlaceCategory.ATTRACTION, tags=None):
    return Place(
        place_id=pid,
        name=name,
        category=category,
        address=f"{name}地址",
        coords=Coordinates(lng=lng, lat=lat),
        city="成都",
        source=PlaceSource.AMAP_POI,
        estimated_duration=120,
        tags=tags or [],
    )


ACTIVITIES = [
    make_place("A001", "宽窄巷子",   104.0534, 30.6711, tags=["街区","古镇"]),
    make_place("A002", "武侯祠",     104.0468, 30.6421, tags=["博物馆","历史遗址"]),
    make_place("A003", "锦里古街",   104.0483, 30.6398, tags=["街区","购物"]),
    make_place("A004", "大熊猫基地", 104.1496, 30.7373, tags=["景区","动物园"]),
    make_place("A005", "都江堰",     103.6171, 31.0044, tags=["景区","5A"]),
    make_place("A006", "东郊记忆",   104.1137, 30.6538, tags=["艺术区","街区"]),
]

FOODS = [
    make_place("F001", "老成都火锅", 104.0500, 30.6600, PlaceCategory.FOOD, tags=["火锅","地方菜"]),
    make_place("F002", "成都串串",   104.0520, 30.6580, PlaceCategory.FOOD, tags=["串串","烧烤"]),
    make_place("F003", "宽巷子茶馆", 104.0530, 30.6700, PlaceCategory.FOOD, tags=["咖啡馆","茶馆"]),
]

HOTELS = [
    make_place("H001", "成都中心酒店", 104.0500, 30.6700, PlaceCategory.HOTEL),
]

ALL_PLACES = ACTIVITIES + FOODS + HOTELS


# ══════════════════════════════════════════════════════════════════════════════
# A2: Schema 验证
# ══════════════════════════════════════════════════════════════════════════════

class TestSchemas:
    def test_group_preferences_defaults(self):
        prefs = GroupPreferences()
        assert prefs.style == "free"
        assert prefs.has_kids is False
        assert prefs.must_have == []
        assert prefs.avoid_outdoor_rain is True

    def test_group_preferences_custom(self):
        prefs = GroupPreferences(style="nightlife", has_kids=True, must_have=["火锅"])
        assert prefs.style == "nightlife"
        assert prefs.has_kids is True
        assert "火锅" in prefs.must_have

    def test_weather_day_defaults(self):
        w = WeatherDay(date="2026-06-01", condition="rainy", precip_mm=12.5)
        assert w.precip_mm == 12.5
        assert w.temp_max == 25.0

    def test_weather_day_sunny(self):
        w = WeatherDay(date="2026-06-01", condition="sunny")
        assert w.precip_mm == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# A3: 鱼骨模板系统
# ══════════════════════════════════════════════════════════════════════════════

class TestTemplates:
    def test_all_five_templates_importable(self):
        from app.agents.planner.templates import (
            T_DEEP_EXPLORE, T_NIGHTLIFE, T_FAMILY_LIGHT, T_ARRIVAL, T_DEPARTURE
        )
        for t in [T_DEEP_EXPLORE, T_NIGHTLIFE, T_FAMILY_LIGHT, T_ARRIVAL, T_DEPARTURE]:
            assert t.template_id
            assert len(t.slots) >= 2

    def test_select_template_arrival_day(self):
        from app.agents.planner.templates import select_template, T_ARRIVAL
        t = select_template(day_index=0, trip_days=3)
        assert t.template_id == T_ARRIVAL.template_id

    def test_select_template_departure_day(self):
        from app.agents.planner.templates import select_template, T_DEPARTURE
        t = select_template(day_index=2, trip_days=3)
        assert t.template_id == T_DEPARTURE.template_id

    def test_select_template_nightlife(self):
        from app.agents.planner.templates import select_template, T_NIGHTLIFE
        prefs = GroupPreferences(style="nightlife")
        t = select_template(day_index=1, trip_days=3, prefs=prefs)
        assert t.template_id == T_NIGHTLIFE.template_id

    def test_select_template_family(self):
        from app.agents.planner.templates import select_template, T_FAMILY_LIGHT
        prefs = GroupPreferences(has_kids=True)
        t = select_template(day_index=1, trip_days=3, prefs=prefs)
        assert t.template_id == T_FAMILY_LIGHT.template_id

    def test_select_template_culture_default(self):
        from app.agents.planner.templates import select_template, T_DEEP_EXPLORE
        prefs = GroupPreferences(style="culture")
        t = select_template(day_index=1, trip_days=3, prefs=prefs)
        assert t.template_id == T_DEEP_EXPLORE.template_id

    def test_all_required_slots_have_l2_candidates(self):
        from app.agents.planner.templates import (
            T_DEEP_EXPLORE, T_NIGHTLIFE, T_FAMILY_LIGHT, T_ARRIVAL, T_DEPARTURE
        )
        for tmpl in [T_DEEP_EXPLORE, T_NIGHTLIFE, T_FAMILY_LIGHT, T_ARRIVAL, T_DEPARTURE]:
            for slot in tmpl.slots:
                assert slot.category_l2_candidates, (
                    f"{tmpl.template_id}.{slot.slot_id} 没有 category_l2_candidates"
                )

    def test_get_template_by_id(self):
        from app.agents.planner.templates import get_template
        t = get_template("T_DEEP_EXPLORE")
        assert t.template_id == "T_DEEP_EXPLORE"

    def test_get_template_invalid_raises(self):
        from app.agents.planner.templates import get_template
        with pytest.raises(KeyError):
            get_template("NONEXISTENT")

    def test_two_day_trip_no_arrival_on_day1(self):
        """2天行程：第0天 → T_ARRIVAL，第1天 → T_DEPARTURE"""
        from app.agents.planner.templates import select_template, T_ARRIVAL, T_DEPARTURE
        assert select_template(0, 2).template_id == T_ARRIVAL.template_id
        assert select_template(1, 2).template_id == T_DEPARTURE.template_id


# ══════════════════════════════════════════════════════════════════════════════
# A4: SchedulerV2 单元测试（mock place_meta DB，离线运行）
# ══════════════════════════════════════════════════════════════════════════════

class TestSchedulerV2:
    """通过直接调用 scheduler_v2.run 验证排线逻辑，mock 掉 DB 和天气调用"""

    def _make_orderings(self, places_per_cluster):
        """构造 {cluster_id: [Place, ...]} 供 scheduler_v2 消费"""
        orderings = {}
        for i, places in enumerate(places_per_cluster):
            ordered = []
            for j, p in enumerate(places):
                ordered.append(p.model_copy(update={"visit_order": j, "cluster_id": i}))
            orderings[i] = ordered
        return orderings

    def _run_scheduler(self, orderings, hotels=None, prefs=None, weather_forecast=None):
        from app.agents.planner.nodes.scheduler_v2 import run

        state = {
            "orderings": orderings,
            "hotels_pool": hotels or [],
            "trip_days": len(orderings),
            "time_matrices": {},
            "user_prefs": prefs,
            "weather_forecast": weather_forecast or {},
            "backup_pool": [],
            "trace": [],
        }

        async def _mock_load_meta(pids):
            return {}  # 全走品类默认值

        with patch("app.agents.planner.nodes.scheduler_v2._load_place_meta", _mock_load_meta):
            with patch("app.agents.planner.nodes.scheduler_v2._fetch_weather", AsyncMock(return_value=None)):
                return asyncio.run(run(state))

    def test_produces_day_plans(self):
        orderings = self._make_orderings([ACTIVITIES[:3], ACTIVITIES[3:]])
        result = self._run_scheduler(orderings)
        assert len(result["day_plans"]) == 2

    def test_produces_day_states(self):
        """day_states 对每天都有 DayPlannerState"""
        orderings = self._make_orderings([ACTIVITIES[:3]])
        result = self._run_scheduler(orderings)
        assert 0 in result["day_states"]
        ds = result["day_states"][0]
        assert "template_id" in ds
        assert "slots" in ds

    def test_hotel_attached(self):
        orderings = self._make_orderings([ACTIVITIES[:3]])
        result = self._run_scheduler(orderings, hotels=HOTELS)
        slots = result["day_states"][0]["slots"]
        hotel_slots = [s for s in slots if s.get("category_l1") == "住宿"]
        assert len(hotel_slots) >= 1, "酒店 check-in slot 未附加"

    def test_backup_pool_collects_overflow(self):
        """超过槽位的地点应进入 backup_pool"""
        # 给一天塞 10 个景点，模板槽位有限，必有溢出
        many_places = [
            make_place(f"X{i:03d}", f"景点{i}", 104.0 + i*0.01, 30.6, tags=["博物馆"])
            for i in range(10)
        ]
        orderings = self._make_orderings([many_places])
        result = self._run_scheduler(orderings)
        # backup_pool 应非空（10个景点 > 模板正常槽位）
        assert len(result["backup_pool"]) > 0

    def test_day_state_has_template_id(self):
        orderings = self._make_orderings([ACTIVITIES[:2]])
        result = self._run_scheduler(orderings)
        ds = result["day_states"][0]
        assert ds["template_id"] in (
            "T_ARRIVAL", "T_DEEP_EXPLORE", "T_NIGHTLIFE",
            "T_FAMILY_LIGHT", "T_DEPARTURE"
        )

    def test_first_day_uses_arrival_template(self):
        orderings = self._make_orderings([ACTIVITIES[:2]])
        result = self._run_scheduler(orderings)
        ds = result["day_states"][0]
        assert ds["template_id"] == "T_ARRIVAL"

    def test_meal_enforcement_creates_fallback_slots(self):
        """无餐饮地点时 _ensure_meal_slots 应插入空占位"""
        from app.agents.planner.nodes.scheduler_v2 import _ensure_meal_slots
        slots = []  # 完全没有餐饮
        result = _ensure_meal_slots(slots, cursor_mins=20 * 60)
        food_slots = [s for s in result if s.get("category_l1") == "餐饮"]
        assert len(food_slots) >= 2  # 午餐 + 晚餐 fallback

    def test_dwell_minutes_fallback(self):
        from app.agents.planner.nodes.scheduler_v2 import _dwell_minutes
        place = make_place("T01", "随便一个博物馆", 104.0, 30.0, tags=["博物馆"])
        mins = _dwell_minutes(place, meta_cache={})
        assert mins == 120  # 博物馆品类默认 120

    def test_outdoor_detection(self):
        from app.agents.planner.nodes.scheduler_v2 import _is_outdoor
        outdoor = make_place("O1", "天府公园", 104.0, 30.0, tags=["公园", "户外"])
        indoor = make_place("I1", "成都博物馆", 104.0, 30.0, tags=["博物馆"])
        assert _is_outdoor(outdoor) is True
        assert _is_outdoor(indoor) is False


# ══════════════════════════════════════════════════════════════════════════════
# A5: WeatherFetcherNode
# ══════════════════════════════════════════════════════════════════════════════

class TestWeatherFetcher:
    def _run(self, state):
        from app.agents.planner.nodes.weather_fetcher import run
        return asyncio.run(run(state))

    def test_skips_without_start_date(self):
        state = {
            "trip_days": 2,
            "center_lat": 30.0, "center_lng": 104.0,
            "trace": [],
        }
        result = self._run(state)
        assert result["weather_forecast"] == {}
        assert "跳过" in " ".join(result["trace"])

    def test_skips_without_api_key(self):
        from unittest.mock import patch
        with patch("app.agents.planner.nodes.weather_fetcher.settings") as mock_settings:
            mock_settings.qweather_api_key = None
            state = {
                "start_date": "2026-06-01",
                "trip_days": 2,
                "center_lat": 30.0, "center_lng": 104.0,
                "trace": [],
            }
            from app.agents.planner.nodes.weather_fetcher import run
            result = asyncio.run(run(state))
            assert result["weather_forecast"] == {}

    def test_handles_invalid_date(self):
        from unittest.mock import patch
        with patch("app.agents.planner.nodes.weather_fetcher.settings") as mock_settings:
            mock_settings.qweather_api_key = "fake_key"
            state = {
                "start_date": "not-a-date",
                "trip_days": 2,
                "center_lat": 30.0, "center_lng": 104.0,
                "trace": [],
            }
            from app.agents.planner.nodes.weather_fetcher import run
            result = asyncio.run(run(state))
            assert result["weather_forecast"] == {}

    def test_parses_mock_api_response(self):
        """模拟和风 API 返回，验证 WeatherDay 解析正确"""
        from datetime import date, timedelta
        from unittest.mock import patch

        today = date.today()
        trip_start = today + timedelta(days=1)  # 明天出发，在 7 日范围内

        mock_daily = [
            {
                "fxDate": trip_start.isoformat(),
                "iconDay": "301",  # 中雨 → rainy, precip=8.0
                "precip": "9.5",
                "tempMax": "28",
                "tempMin": "20",
                "sunrise": "06:15",
                "sunset": "19:30",
            }
        ]

        async def mock_fetch(*args, **kwargs):
            return mock_daily

        with patch("app.agents.planner.nodes.weather_fetcher._fetch_qweather_7d", mock_fetch):
            with patch("app.agents.planner.nodes.weather_fetcher.settings") as mock_settings:
                mock_settings.qweather_api_key = "fake_key"
                state = {
                    "start_date": trip_start.isoformat(),
                    "trip_days": 1,
                    "center_lat": 30.67, "center_lng": 104.05,
                    "trace": [],
                }
                from app.agents.planner.nodes.weather_fetcher import run
                result = asyncio.run(run(state))

        assert 0 in result["weather_forecast"]
        wd = result["weather_forecast"][0]
        assert wd.condition == "rainy"
        assert wd.precip_mm == 9.5
        assert wd.temp_max == 28.0
        assert wd.sunrise == "06:15"


# ══════════════════════════════════════════════════════════════════════════════
# A6: Critic v2 全图 mock run
# ══════════════════════════════════════════════════════════════════════════════

class TestCriticV2Integration:
    """直接调用规则检查函数（无 DB），验证多规则组合场景"""

    def _make_slot(self, pid, l1, l2, start, end, place_tags=None):
        return {
            "slot_index": 0,
            "template_slot_id": "test",
            "place_id": pid,
            "place": {
                "place_id": pid, "name": pid,
                "category": "attraction" if l1 == "景点" else "food",
                "address": "addr", "coords": {"lng": 104.0, "lat": 30.0},
                "city": "成都", "tags": place_tags or [],
            },
            "start_time": start,
            "end_time": end,
            "category_l1": l1,
            "category_l2": l2,
            "is_required": True,
        }

    def _run_all_rules(self, slots, weather=None, meta_cache=None, dow=1):
        """直接调用所有规则函数并汇总违规（不走 DB）"""
        from app.agents.planner.nodes.critic_v2 import (
            _check_no_backtoback_l2,
            _check_meal_slot_filled,
            _check_daily_food_cap,
            _check_zero_food_day,
            _check_weather_mismatch,
            _check_buffer_deficit,
            _check_open_hours,
        )
        mc = meta_cache or {}
        violations = []
        for s in slots:
            v = _check_open_hours(s, 0, mc, dow)
            if v:
                violations.append(v)
        violations.extend(_check_no_backtoback_l2(slots, 0))
        violations.extend(_check_meal_slot_filled(slots, 0))
        v = _check_daily_food_cap(slots, 0)
        if v:
            violations.append(v)
        v = _check_zero_food_day(slots, 0)
        if v:
            violations.append(v)
        v = _check_weather_mismatch(slots, 0, weather)
        if v:
            violations.append(v)
        violations.extend(_check_buffer_deficit(slots, 0))
        return violations

    def test_detects_no_violations_on_clean_schedule(self):
        slots = [
            self._make_slot("a1", "景点", "博物馆", "09:00", "11:00"),
            self._make_slot("r1", "餐饮", "餐厅",   "12:00", "13:00"),
            self._make_slot("a2", "景点", "街区",   "14:00", "15:30"),
            self._make_slot("r2", "餐饮", "餐厅",   "18:30", "19:30"),
        ]
        violations = self._run_all_rules(slots)
        non_buffer = [v for v in violations if v["rule"] != "R_BUFFER_DEFICIT"]
        assert non_buffer == [], f"未预期违规: {non_buffer}"

    def test_detects_zero_food_day(self):
        slots = [
            self._make_slot("a1", "景点", "博物馆", "09:00", "11:00"),
            self._make_slot("a2", "景点", "街区",   "14:00", "15:30"),
        ]
        violations = self._run_all_rules(slots)
        rules = [v["rule"] for v in violations]
        assert "R_ZERO_FOOD_DAY" in rules

    def test_detects_backtoback_l2(self):
        slots = [
            self._make_slot("r1", "餐饮", "餐厅", "12:00", "13:00"),
            self._make_slot("r2", "餐饮", "餐厅", "18:30", "19:30"),
            self._make_slot("f1", "餐饮", "火锅", "12:00", "13:30"),
            self._make_slot("f2", "餐饮", "火锅", "19:30", "21:00"),  # 火锅连排
        ]
        violations = self._run_all_rules(slots)
        rules = [v["rule"] for v in violations]
        assert "R_NO_BACKTOBACK_L2" in rules

    def test_detects_weather_mismatch(self):
        rain = WeatherDay(date="2026-06-09", condition="rainy", precip_mm=20.0)
        slots = [
            self._make_slot("o1", "景点", "景区", "09:00", "11:00", place_tags=["景区", "户外"]),
            self._make_slot("o2", "景点", "公园", "13:00", "14:30", place_tags=["公园", "户外"]),
            self._make_slot("r1", "餐饮", "餐厅", "12:00", "13:00"),
            self._make_slot("r2", "餐饮", "餐厅", "18:30", "19:30"),
        ]
        violations = self._run_all_rules(slots, weather=rain)
        rules = [v["rule"] for v in violations]
        assert "R_WEATHER_MISMATCH" in rules

    def test_combined_violations(self):
        """多规则同时触发"""
        slots = [
            self._make_slot("f1", "餐饮", "火锅", "12:00", "13:30"),
            self._make_slot("f2", "餐饮", "火锅", "19:30", "21:00"),  # 连排火锅
            # 无 R_MEAL_SLOT_FILLED 违规（12:00 是午饭，19:30 是晚饭）
        ]
        violations = self._run_all_rules(slots)
        rules = {v["rule"] for v in violations}
        assert "R_NO_BACKTOBACK_L2" in rules  # 火锅连排


# ══════════════════════════════════════════════════════════════════════════════
# 图拓扑 / v2 节点验证
# ══════════════════════════════════════════════════════════════════════════════

class TestPlannerGraphV2:
    def test_graph_compiles(self):
        from app.agents.planner.graph import build_planner_graph
        g = build_planner_graph()
        assert g is not None

    def test_graph_has_v2_nodes(self):
        from app.agents.planner.graph import build_planner_graph
        g = build_planner_graph()
        nodes = list(g.nodes)
        assert "scheduler_v2"    in nodes, "scheduler_v2 节点缺失"
        assert "weather_fetcher" in nodes, "weather_fetcher 节点缺失"
        assert "critic_v2"       in nodes, "critic_v2 节点缺失"

    def test_graph_v1_scheduler_removed(self):
        """确认旧 scheduler 节点不再在图中（已被 scheduler_v2 替换）"""
        from app.agents.planner.graph import build_planner_graph
        g = build_planner_graph()
        # 旧 v1 节点名是 "scheduler"，v2 图中不应该出现
        assert "scheduler" not in list(g.nodes), "旧 scheduler v1 节点仍在图中"

    def test_planner_result_is_named_tuple(self):
        from app.agents.planner.graph import PlannerResult
        r = PlannerResult(itinerary=None, backup_pool=[], critic_violations=[])  # type: ignore
        assert hasattr(r, "itinerary")
        assert hasattr(r, "backup_pool")
        assert hasattr(r, "critic_violations")

    def test_run_planner_end_to_end(self):
        """端到端：mock DB + 天气，验证返回 PlannerResult 结构完整"""
        from app.agents.planner.graph import run_planner
        import app.agents.planner.nodes.critic_v2 as cv2

        async def patched_critic(state):
            return {"critic_violations": [], "trace": state.get("trace", []) + ["[CriticV2] mocked"]}

        async def _run():
            with patch("app.agents.planner.nodes.scheduler_v2._load_place_meta", AsyncMock(return_value={})):
                with patch("app.agents.planner.nodes.weather_fetcher._fetch_qweather_7d", AsyncMock(return_value=[])):
                    orig_critic = cv2.run
                    cv2.run = patched_critic
                    try:
                        return await run_planner(
                            places=ALL_PLACES,
                            trip_days=2,
                            thread_id="phase-a-test-001",
                        )
                    finally:
                        cv2.run = orig_critic

        result = asyncio.run(_run())
        assert result.itinerary is not None
        assert isinstance(result.backup_pool, list)
        assert isinstance(result.critic_violations, list)
        assert len(result.itinerary.days) == 2

    def test_run_planner_with_user_prefs(self):
        """传入 GroupPreferences 时 run_planner 不报错"""
        from app.agents.planner.graph import run_planner
        import app.agents.planner.nodes.critic_v2 as cv2

        prefs = GroupPreferences(style="nightlife", trip_city="成都", trip_days=2)

        async def patched_critic(state):
            return {"critic_violations": [], "trace": []}

        async def _run():
            with patch("app.agents.planner.nodes.scheduler_v2._load_place_meta", AsyncMock(return_value={})):
                with patch("app.agents.planner.nodes.weather_fetcher._fetch_qweather_7d", AsyncMock(return_value=[])):
                    orig = cv2.run
                    cv2.run = patched_critic
                    try:
                        return await run_planner(
                            places=ALL_PLACES,
                            trip_days=2,
                            thread_id="phase-a-test-002",
                            user_prefs=prefs,
                        )
                    finally:
                        cv2.run = orig

        result = asyncio.run(_run())
        assert result.itinerary is not None


# ══════════════════════════════════════════════════════════════════════════════
# API Schema 验证
# ══════════════════════════════════════════════════════════════════════════════

class TestAPISchema:
    def test_optimize_response_has_backup_pool(self):
        from app.schemas.api import OptimizeResponse
        fields = OptimizeResponse.model_fields
        assert "backup_pool" in fields, "OptimizeResponse 缺少 backup_pool 字段"
        assert "critic_violations" in fields, "OptimizeResponse 缺少 critic_violations 字段"

    def test_optimize_response_backup_pool_default_empty(self):
        from app.schemas.itinerary import Itinerary
        from app.schemas.api import OptimizeResponse
        from datetime import datetime, timezone

        dummy_itinerary = Itinerary(
            itinerary_id="test-id",
            thread_id="t1",
            city="成都",
            days=[],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        resp = OptimizeResponse(
            itinerary=dummy_itinerary,
            total_distance_km=0.0,
            duration_ms=100,
        )
        assert resp.backup_pool == []
        assert resp.critic_violations == []
