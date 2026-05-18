"""
天气查询工具

封装和风天气 API 为 LangChain @tool。
ReAct Agent 在用户询问出行建议、穿衣推荐、是否适合出行时调用。

配置（.env）：
  QWEATHER_API_KEY=...
  QWEATHER_AUTH_TYPE=apikey   # 或 "jwt"
"""

import json
from typing import Annotated

from langchain_core.tools import tool


@tool
async def get_weather(
    city: Annotated[str, "城市名称，如'成都''北京'"],
    days: Annotated[int, "查询未来几天天气，1-7"] = 3,
) -> str:
    """
    查询目的地未来天气预报（温度、天气状况、出行建议）。

    当用户询问：
    - 最近天气怎么样
    - 需要带伞吗
    - 适合什么季节去
    - 穿什么衣服合适
    时，调用此工具。

    返回：未来几天的天气预报，JSON 格式。
    """
    from app.config import settings

    # 无 API Key 时返回模拟数据（不影响主流程）
    if not settings.qweather_api_key:
        mock_data = _mock_weather(city, days)
        return json.dumps(mock_data, ensure_ascii=False)

    try:
        import aiohttp

        headers = await _build_weather_headers()
        url = f"https://{settings.qweather_api_host}/v7/weather/{days}d"

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params={"location": city, "lang": "zh"},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()

        if data.get("code") != "200":
            return json.dumps(_mock_weather(city, days), ensure_ascii=False)

        forecasts = []
        for day in data.get("daily", [])[:days]:
            forecasts.append({
                "date": day.get("fxDate"),
                "weather": day.get("textDay"),
                "temp_high": day.get("tempMax"),
                "temp_low": day.get("tempMin"),
                "wind": day.get("windDirDay"),
                "suggestion": _make_suggestion(day),
            })

        return json.dumps(
            {"status": "ok", "city": city, "forecast": forecasts},
            ensure_ascii=False, indent=2,
        )

    except Exception as exc:
        return json.dumps(_mock_weather(city, days), ensure_ascii=False)


def _make_suggestion(day: dict) -> str:
    """根据天气数据生成出行建议"""
    text = day.get("textDay", "")
    if "雨" in text:
        return "有雨，建议携带雨具，避开户外长时间行程"
    if "雪" in text:
        return "有雪，注意防滑保暖，部分景点可能关闭"
    if "晴" in text:
        temp = int(day.get("tempMax", 25))
        if temp > 35:
            return "晴天高温，注意防晒补水，避开午间户外活动"
        return "晴天，适合户外游览"
    if "雾" in text or "霾" in text:
        return "能见度低，开车注意安全，呼吸系统敏感者建议减少外出"
    return "天气一般，正常出行"


def _mock_weather(city: str, days: int) -> dict:
    """天气 API 不可用时的模拟数据"""
    import random
    weathers = ["晴", "多云", "阵雨", "阴"]
    forecasts = []
    for i in range(days):
        from datetime import date, timedelta
        d = date.today() + timedelta(days=i)
        w = weathers[i % len(weathers)]
        forecasts.append({
            "date": d.isoformat(),
            "weather": w,
            "temp_high": random.randint(18, 32),
            "temp_low": random.randint(12, 22),
            "suggestion": _make_suggestion({"textDay": w, "tempMax": "28"}),
        })
    return {"status": "mock", "city": city, "forecast": forecasts}


async def _build_weather_headers() -> dict:
    """构建和风天气 API 请求头（支持 JWT 和 API Key 两种认证）"""
    from app.config import settings

    if settings.qweather_auth_type == "jwt" and settings.qweather_private_key:
        import time
        import jwt as pyjwt  # PyJWT
        payload = {
            "sub": settings.qweather_project_id,
            "iat": int(time.time()) - 30,
            "exp": int(time.time()) + 900,
        }
        private_key = (
            "-----BEGIN PRIVATE KEY-----\n"
            + settings.qweather_private_key
            + "\n-----END PRIVATE KEY-----"
        )
        token = pyjwt.encode(
            payload,
            private_key,
            algorithm="EdDSA",
            headers={"kid": settings.qweather_key_id},
        )
        return {"Authorization": f"Bearer {token}"}

    # API Key 模式
    return {"X-QW-Api-Key": settings.qweather_api_key}
