"""硬规则单测（SPEC §3.6 / A8）

构造必出 bug 的 fixture，验证 Critic v2 能正确拦截违规。
无需数据库 / 外部 API，全部离线运行。
"""


from app.agents.planner.nodes.critic_v2 import (
    _check_no_backtoback_l2,
    _check_meal_slot_filled,
    _check_daily_food_cap,
    _check_zero_food_day,
    _check_weather_mismatch,
    _check_buffer_deficit,
    _check_open_hours,
)
from app.agents.planner.state import Slot
from app.schemas.preferences import WeatherDay


# ─── Fixture 辅助 ──────────────────────────────────────────────────────────────

def make_slot(
    place_id: str,
    category_l1: str,
    category_l2: str,
    start: str,
    end: str,
    place_tags: list[str] | None = None,
) -> Slot:
    place_dict = {
        "place_id": place_id,
        "name": place_id,
        "category": "attraction" if category_l1 == "景点" else "food",
        "address": "测试地址",
        "coords": {"lng": 104.0, "lat": 30.0},
        "city": "成都",
        "tags": place_tags or [],
    }
    return {
        "slot_index": 0,
        "template_slot_id": "test",
        "place_id": place_id,
        "place": place_dict,
        "start_time": start,
        "end_time": end,
        "category_l1": category_l1,
        "category_l2": category_l2,
        "is_required": True,
    }


# ─── R_NO_BACKTOBACK_L2 ────────────────────────────────────────────────────────

class TestNoBackToBackL2:
    def test_catches_consecutive_hotpot(self):
        slots = [
            make_slot("p1", "餐饮", "火锅", "12:00", "13:30"),
            make_slot("p2", "餐饮", "火锅", "19:00", "20:30"),  # 同火锅连排
        ]
        violations = _check_no_backtoback_l2(slots, day_index=0)
        assert len(violations) == 1
        assert violations[0]["rule"] == "R_NO_BACKTOBACK_L2"

    def test_allows_different_l2(self):
        slots = [
            make_slot("p1", "景点", "博物馆", "09:00", "11:00"),
            make_slot("p2", "餐饮", "火锅",   "12:00", "13:30"),
            make_slot("p3", "景点", "街区",   "14:00", "15:30"),
        ]
        violations = _check_no_backtoback_l2(slots, day_index=0)
        assert violations == []

    def test_catches_consecutive_museums(self):
        slots = [
            make_slot("m1", "景点", "博物馆", "09:00", "11:00"),
            make_slot("m2", "景点", "博物馆", "11:30", "13:00"),
        ]
        violations = _check_no_backtoback_l2(slots, day_index=1)
        assert len(violations) >= 1


# ─── R_MEAL_SLOT_FILLED ───────────────────────────────────────────────────────

class TestMealSlotFilled:
    def test_catches_missing_lunch(self):
        slots = [
            make_slot("a1", "景点", "博物馆", "09:00", "11:00"),
            make_slot("r1", "餐饮", "餐厅",   "19:00", "20:00"),  # 只有晚餐
        ]
        violations = _check_meal_slot_filled(slots, day_index=0)
        rules = [v["rule"] for v in violations]
        assert "R_MEAL_SLOT_FILLED" in rules
        msgs = [v["message"] for v in violations]
        assert any("午餐" in m for m in msgs)

    def test_catches_missing_dinner(self):
        slots = [
            make_slot("r1", "餐饮", "餐厅", "12:30", "13:30"),  # 只有午餐
        ]
        violations = _check_meal_slot_filled(slots, day_index=0)
        msgs = [v["message"] for v in violations]
        assert any("晚餐" in m for m in msgs)

    def test_passes_with_both_meals(self):
        slots = [
            make_slot("r1", "餐饮", "餐厅", "12:00", "13:00"),
            make_slot("r2", "餐饮", "餐厅", "18:30", "19:30"),
        ]
        violations = _check_meal_slot_filled(slots, day_index=0)
        assert violations == []


# ─── R_DAILY_FOOD_CAP ─────────────────────────────────────────────────────────

class TestDailyFoodCap:
    def test_catches_four_food_slots(self):
        slots = [
            make_slot("r1", "餐饮", "餐厅", "08:00", "09:00"),
            make_slot("r2", "餐饮", "餐厅", "12:00", "13:00"),
            make_slot("r3", "餐饮", "火锅", "15:00", "16:30"),
            make_slot("r4", "餐饮", "餐厅", "18:30", "19:30"),
        ]
        v = _check_daily_food_cap(slots, day_index=0)
        assert v is not None
        assert v["rule"] == "R_DAILY_FOOD_CAP"

    def test_passes_three_food_slots(self):
        slots = [
            make_slot("r1", "餐饮", "餐厅", "08:00", "09:00"),
            make_slot("r2", "餐饮", "餐厅", "12:00", "13:00"),
            make_slot("r3", "餐饮", "餐厅", "18:30", "19:30"),
        ]
        assert _check_daily_food_cap(slots, day_index=0) is None


# ─── R_ZERO_FOOD_DAY ──────────────────────────────────────────────────────────

class TestZeroFoodDay:
    def test_catches_no_food(self):
        slots = [
            make_slot("a1", "景点", "博物馆", "09:00", "11:30"),
            make_slot("a2", "景点", "街区",   "13:00", "14:30"),
        ]
        v = _check_zero_food_day(slots, day_index=0)
        assert v is not None
        assert v["rule"] == "R_ZERO_FOOD_DAY"

    def test_passes_with_food(self):
        slots = [
            make_slot("a1", "景点", "博物馆", "09:00", "11:30"),
            make_slot("r1", "餐饮", "餐厅",   "12:00", "13:00"),
        ]
        assert _check_zero_food_day(slots, day_index=0) is None


# ─── R_WEATHER_MISMATCH ───────────────────────────────────────────────────────

class TestWeatherMismatch:
    def test_catches_rainy_outdoor(self):
        rain = WeatherDay(date="2026-06-01", condition="rainy", precip_mm=10.0)
        slots = [
            make_slot("p1", "景点", "景区", "09:00", "11:00", place_tags=["户外","景区"]),
            make_slot("p2", "景点", "公园", "14:00", "15:30", place_tags=["公园","户外"]),
        ]
        v = _check_weather_mismatch(slots, day_index=0, weather=rain)
        assert v is not None
        assert v["rule"] == "R_WEATHER_MISMATCH"

    def test_passes_light_rain(self):
        light_rain = WeatherDay(date="2026-06-01", condition="rainy", precip_mm=3.0)
        slots = [
            make_slot("p1", "景点", "景区", "09:00", "11:00", place_tags=["户外"]),
            make_slot("p2", "景点", "公园", "14:00", "15:30", place_tags=["公园"]),
        ]
        v = _check_weather_mismatch(slots, day_index=0, weather=light_rain)
        assert v is None

    def test_passes_rainy_indoor(self):
        rain = WeatherDay(date="2026-06-01", condition="rainy", precip_mm=15.0)
        slots = [
            make_slot("p1", "景点", "博物馆", "09:00", "11:00"),
            make_slot("p2", "餐饮", "餐厅",   "12:00", "13:00"),
        ]
        v = _check_weather_mismatch(slots, day_index=0, weather=rain)
        assert v is None


# ─── R_BUFFER_DEFICIT ─────────────────────────────────────────────────────────

class TestBufferDeficit:
    def test_catches_overlap(self):
        slots = [
            make_slot("p1", "景点", "博物馆", "09:00", "12:30"),
            make_slot("p2", "餐饮", "餐厅",   "12:00", "13:00"),  # 提前开始（重叠）
        ]
        violations = _check_buffer_deficit(slots, day_index=0)
        assert len(violations) >= 1
        assert violations[0]["rule"] == "R_BUFFER_DEFICIT"

    def test_passes_no_overlap(self):
        slots = [
            make_slot("p1", "景点", "博物馆", "09:00", "11:30"),
            make_slot("p2", "餐饮", "餐厅",   "12:00", "13:00"),
        ]
        violations = _check_buffer_deficit(slots, day_index=0)
        assert violations == []


# ─── R_OPEN_HOURS（用 meta_cache mock） ────────────────────────────────────────

class TestOpenHours:
    def test_catches_closed_day(self):
        meta_cache = {
            "museum_01": {
                "open_hours_json": {
                    "mon": None,  # 周一闭馆
                    "tue": [[9, 17]], "wed": [[9, 17]], "thu": [[9, 17]],
                    "fri": [[9, 17]], "sat": [[9, 17]], "sun": [[9, 17]],
                }
            }
        }
        slot = make_slot("museum_01", "景点", "博物馆", "10:00", "12:00")
        v = _check_open_hours(slot, day_index=0, meta_cache=meta_cache, dow=0)  # dow=0 周一
        assert v is not None
        assert v["rule"] == "R_OPEN_HOURS"

    def test_catches_outside_hours(self):
        meta_cache = {
            "cafe_01": {
                "open_hours_json": {
                    "mon": [[9, 21]], "tue": [[9, 21]], "wed": [[9, 21]],
                    "thu": [[9, 21]], "fri": [[9, 21]], "sat": [[9, 21]],
                    "sun": [[9, 21]],
                }
            }
        }
        slot = make_slot("cafe_01", "餐饮", "咖啡馆", "22:00", "23:00")
        v = _check_open_hours(slot, day_index=0, meta_cache=meta_cache, dow=2)
        assert v is not None
        assert v["rule"] == "R_OPEN_HOURS"

    def test_passes_within_hours(self):
        meta_cache = {
            "rest_01": {
                "open_hours_json": {
                    "wed": [[11, 22]],
                }
            }
        }
        slot = make_slot("rest_01", "餐饮", "餐厅", "12:00", "13:00")
        v = _check_open_hours(slot, day_index=0, meta_cache=meta_cache, dow=2)
        assert v is None
