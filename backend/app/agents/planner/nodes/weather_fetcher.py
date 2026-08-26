"""WeatherFetcherNode：预拉行程各天天气，写入 PlannerState.weather_forecast（SPEC §3.3/A5）

在 sequencer → scheduler_v2 之间插入。
依赖：
  - state.start_date      — 出发日期 ISO 8601（可选）
  - state.center_lat/lng  — 全局质心（ClustererAgent 产出）
  - state.trip_days       — 天数

输出：
  - weather_forecast: dict[int, WeatherDay]  — day_index → 天气摘要
    · 有和风 API key 且 start_date 在未来 7 天内 → 调 API
    · 否则 → 空字典（scheduler_v2 会安全降级：无天气数据时不过滤户外）

天气适配逻辑（SPEC §3.3 第5步）：
  - 雨天 precip_mm > 5mm  → 户外槽换室内
  - 夏季 11:30–14:00 户外 → 改室内/餐厅
  - 冬季日落后         → 后置槽切换室内/夜景
（以上适配由 scheduler_v2 消费 weather_forecast 实现，本节点只负责拉数据）
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import aiohttp

from app.agents.planner.state import PlannerState
from app.config import settings
from app.schemas.preferences import WeatherDay
from app.tools.weather_tool import _build_weather_headers

# 和风天气 API 条件码 → 统一 condition 字符串 + precip_mm 估算
_QWEATHER_COND_MAP: dict[str, tuple[str, float]] = {
    "100": ("sunny",   0.0),
    "101": ("cloudy",  0.0),
    "102": ("cloudy",  0.0),
    "103": ("cloudy",  0.0),
    "104": ("cloudy",  0.0),
    "300": ("rainy",   3.0),
    "301": ("rainy",   8.0),
    "302": ("rainy",  15.0),
    "303": ("rainy",  25.0),
    "304": ("rainy",   5.0),
    "305": ("rainy",   3.0),
    "306": ("rainy",   8.0),
    "307": ("rainy",  15.0),
    "308": ("rainy",  25.0),
    "309": ("rainy",   2.0),
    "310": ("rainy",  30.0),
    "311": ("rainy",  40.0),
    "312": ("rainy",  50.0),
    "400": ("snowy",   0.0),
    "401": ("snowy",   0.0),
    "402": ("snowy",   0.0),
    "403": ("snowy",   0.0),
    "500": ("foggy",   0.0),
    "501": ("foggy",   0.0),
}


def _parse_condition(code: str, precip_str: str) -> tuple[str, float]:
    """从和风条件码解析 condition 和降水量"""
    condition, default_precip = _QWEATHER_COND_MAP.get(code, ("cloudy", 0.0))
    try:
        precip = float(precip_str) if precip_str else default_precip
    except ValueError:
        precip = default_precip
    return condition, precip


def _has_qweather_credentials() -> bool:
    """Match the API weather adapter's JWT/API-key credential contract."""
    if settings.qweather_auth_type == "jwt":
        return bool(
            settings.qweather_private_key
            and settings.qweather_key_id
            and settings.qweather_project_id
        )
    return bool(settings.qweather_api_key)


async def _fetch_qweather_7d(
    session: aiohttp.ClientSession,
    lat: float,
    lng: float,
) -> list[dict]:
    """调和风 7 日预报 API，返回 daily 列表；失败返回空"""
    if not _has_qweather_credentials():
        return []
    try:
        host = (
            settings.qweather_api_host.strip()
            .removeprefix("https://")
            .removeprefix("http://")
            .rstrip("/")
        )
        url = f"https://{host}/v7/weather/7d"
        params = {
            "location": f"{lng:.4f},{lat:.4f}",
            "lang": "zh",
        }
        headers = await _build_weather_headers()
        async with session.get(
            url,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json()
            if data.get("code") == "200":
                return data.get("daily", [])
    except Exception as e:
        print(f"[WeatherFetcher] 和风 API 失败（降级无天气）：{e}")
    return []


async def run(state: PlannerState) -> dict:
    start_date_str: Optional[str] = state.get("start_date")
    center_lat: float = state.get("center_lat", 0.0)
    center_lng: float = state.get("center_lng", 0.0)
    trip_days: int = state.get("trip_days", 1)

    weather_forecast: dict[int, WeatherDay] = {}

    if not start_date_str or not _has_qweather_credentials():
        trace = state.get("trace", []) + [
            "[WeatherFetcher] 跳过（无 start_date 或无 Provider 凭据）"
        ]
        return {"weather_forecast": weather_forecast, "trace": trace}

    try:
        trip_start = date.fromisoformat(start_date_str)
    except ValueError:
        trace = state.get("trace", []) + ["[WeatherFetcher] start_date 格式错误，跳过"]
        return {"weather_forecast": weather_forecast, "trace": trace}

    today = date.today()
    offset_start = (trip_start - today).days
    if offset_start > 7 or offset_start < -1:
        trace = state.get("trace", []) + [
            f"[WeatherFetcher] 出发日期 {trip_start} 超出 7 日预报范围，跳过"
        ]
        return {"weather_forecast": weather_forecast, "trace": trace}

    async with aiohttp.ClientSession() as session:
        daily_list = await _fetch_qweather_7d(session, center_lat, center_lng)

    # 建立 date → daily_dict 索引
    date_map: dict[str, dict] = {}
    for item in daily_list:
        fxDate = item.get("fxDate", "")
        if fxDate:
            date_map[fxDate] = item

    for day_index in range(trip_days):
        day_date = trip_start + timedelta(days=day_index)
        date_str = day_date.isoformat()
        item = date_map.get(date_str)

        if item is None:
            continue

        condition, precip = _parse_condition(
            item.get("iconDay", "101"),
            item.get("precip", ""),
        )

        sunrise = item.get("sunrise", "06:00")
        sunset = item.get("sunset", "18:30")

        try:
            temp_max = float(item.get("tempMax", 25))
            temp_min = float(item.get("tempMin", 15))
        except ValueError:
            temp_max, temp_min = 25.0, 15.0

        weather_forecast[day_index] = WeatherDay(
            date=date_str,
            condition=condition,
            precip_mm=precip,
            temp_max=temp_max,
            temp_min=temp_min,
            sunrise=sunrise,
            sunset=sunset,
        )

    trace = state.get("trace", []) + [
        f"[WeatherFetcher] 获取到 {len(weather_forecast)}/{trip_days} 天天气预报"
    ]
    return {"weather_forecast": weather_forecast, "trace": trace}
