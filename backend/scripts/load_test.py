"""
BreezeTravel 压测脚本（Locust）

场景
----
1. ChatUser    : POST /api/chat (SSE 流) - 模拟 AI 对话，DEMO_MODE 下无 LLM 调用
2. OptimizeUser: POST /api/optimize      - 模拟路线优化，纯算法无 LLM
3. HealthUser  : GET  /health            - 健康探针基线对照

运行方式
--------
  # 安装
  pip install locust

  # 无界面模式（推荐 CI/基准测试）：50 并发，持续 60s
  locust -f backend/scripts/load_test.py --headless \
         -u 50 -r 10 --run-time 60s \
         --host http://localhost:8000 \
         --csv backend/results/load_test

  # 带 Web UI（实时观察）：访问 http://localhost:8089
  locust -f backend/scripts/load_test.py --host http://localhost:8000

  # 仅测 optimize（不需要 DEMO_MODE）
  locust -f backend/scripts/load_test.py \
         --headless -u 20 -r 5 --run-time 30s \
         --host http://localhost:8000 \
         -T OptimizeUser,HealthUser \
         --csv backend/results/load_test_optimize

注意事项
--------
- 压测 /api/chat 前必须先启动 backend 并设置 DEMO_MODE=true
  否则每个请求都会真实调用 DeepSeek API，产生费用
- 压测结果 CSV 保存在 backend/results/load_test_*.csv
- 结果文件可用于在 README 中展示 P95 延迟和最大 QPS
"""

import json
import random
import time
import uuid

# locust 仅在完整压测模式下才需要，quick 模式可无需安装
try:
    from locust import HttpUser, between, task
    _LOCUST_AVAILABLE = True
except ImportError:
    _LOCUST_AVAILABLE = False
    # 定义占位符，让文件其余部分可以正常解析
    class HttpUser:  # type: ignore
        pass
    def between(a, b):  # type: ignore
        return None
    def task(w=1):  # type: ignore
        return lambda f: f

# ═══════════════════════════════════════════════════════════════════
# 测试数据
# ═══════════════════════════════════════════════════════════════════

_CITIES = ["成都", "北京", "上海", "厦门", "广州", "深圳", "杭州"]

_CHAT_QUERIES = [
    "推荐几个{city}的特色美食",
    "{city}有哪些值得打卡的景点",
    "{city}旅游三天怎么安排",
    "第一次去{city}需要注意什么",
    "{city}有什么好玩的地方推荐",
    "{city}适合亲子游的地方有哪些",
    "{city}有哪些网红打卡点",
    "{city}最值得去的景点排行",
]

# 用于 /api/optimize 的样本地点（成都，无需真实坐标因为 AMAP_MOCK=true）
_SAMPLE_PLACES = [
    {
        "place_id": "B0FFG5M28B", "name": "宽窄巷子", "category": "attraction",
        "address": "青羊区宽巷子3号", "coords": {"lng": 104.0587, "lat": 30.6719},
        "city": "成都", "source": "synthesized",
        "amap_rating": 4.5, "description": "成都最具代表性的历史文化街区",
        "tags": ["文化", "拍照", "必打卡"], "estimated_duration": 120,
    },
    {
        "place_id": "B0FFG5SKNS", "name": "锦里古街", "category": "attraction",
        "address": "武侯区武侯祠大街231号", "coords": {"lng": 104.0436, "lat": 30.6427},
        "city": "成都", "source": "synthesized",
        "amap_rating": 4.4, "description": "以三国文化为主题的仿古商业街",
        "tags": ["历史", "美食", "夜市"], "estimated_duration": 90,
    },
    {
        "place_id": "B0FFG5MKXY", "name": "成都大熊猫繁育研究基地", "category": "attraction",
        "address": "成华区熊猫大道1375号", "coords": {"lng": 104.1400, "lat": 30.7376},
        "city": "成都", "source": "synthesized",
        "amap_rating": 4.7, "description": "全球最大的大熊猫繁育机构",
        "tags": ["亲子", "自然", "熊猫"], "estimated_duration": 180,
    },
    {
        "place_id": "B0FFG4SSFR", "name": "都江堰景区", "category": "attraction",
        "address": "都江堰市灌县古城路", "coords": {"lng": 103.6090, "lat": 30.9970},
        "city": "成都", "source": "synthesized",
        "amap_rating": 4.6, "description": "2000年历史的世界文化遗产水利工程",
        "tags": ["世遗", "历史", "自然"], "estimated_duration": 150,
    },
    {
        "place_id": "B0FFH1XYZA", "name": "玉林路小酒馆", "category": "food",
        "address": "武侯区玉林路15号", "coords": {"lng": 104.0631, "lat": 30.6498},
        "city": "成都", "source": "synthesized",
        "amap_rating": 4.3, "description": "成都著名的文艺酒馆聚集地",
        "tags": ["夜生活", "文艺", "打卡"], "estimated_duration": 90,
    },
    {
        "place_id": "B0FFG5LMNO", "name": "九寨沟景区", "category": "attraction",
        "address": "阿坝州九寨沟县漳扎镇", "coords": {"lng": 103.9191, "lat": 33.2198},
        "city": "成都", "source": "synthesized",
        "amap_rating": 4.8, "description": "世界自然遗产，以多彩湖泊著称",
        "tags": ["自然", "摄影", "绝景"], "estimated_duration": 480,
    },
]


def _random_chat_payload(city: str = None) -> dict:
    city = city or random.choice(_CITIES)
    query = random.choice(_CHAT_QUERIES).format(city=city)
    return {
        "thread_id": f"load-test-{uuid.uuid4().hex[:8]}",
        "user_id": "load-test-user",
        "message": query,
        "trip_city": city,
        "selected_place_ids": [],
    }


def _random_optimize_payload(n_places: int = None) -> dict:
    n = n_places or random.randint(3, 6)
    places = random.sample(_SAMPLE_PLACES, min(n, len(_SAMPLE_PLACES)))
    return {
        "thread_id": f"load-test-opt-{uuid.uuid4().hex[:8]}",
        "places": places,
        "trip_days": random.randint(2, 4),
    }


# ═══════════════════════════════════════════════════════════════════
# SSE 辅助：读取流并计算首帧延迟
# ═══════════════════════════════════════════════════════════════════

def _consume_sse(response, timeout: float = 15.0) -> dict:
    """
    消费 SSE 响应流，返回统计数据：
    - ttfb_ms     : 首帧延迟（首个 SSE 事件到达时间）
    - total_ms    : 流完成总耗时
    - total_places: 最终 done 事件中的地点数
    - events_count: SSE 事件总数
    """
    stats = {
        "ttfb_ms": None,
        "total_ms": 0,
        "total_places": 0,
        "events_count": 0,
        "success": False,
    }
    t0 = time.perf_counter()
    try:
        for line in response.iter_lines(chunk_size=None):
            if not line:
                continue
            if line.startswith("data: "):
                payload_str = line[6:]
                stats["events_count"] += 1
                if stats["ttfb_ms"] is None:
                    stats["ttfb_ms"] = int((time.perf_counter() - t0) * 1000)
                try:
                    payload = json.loads(payload_str)
                    event_type = payload.get("event")
                    if event_type == "done":
                        stats["total_places"] = payload.get("data", {}).get("total_places", 0)
                        stats["success"] = True
                        break
                    elif event_type == "error":
                        break
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    stats["total_ms"] = int((time.perf_counter() - t0) * 1000)
    return stats


# ═══════════════════════════════════════════════════════════════════
# Locust 用户类
# ═══════════════════════════════════════════════════════════════════

class ChatUser(HttpUser):
    """
    模拟 AI 对话用户（SSE 流）

    DEMO_MODE=true 时：无 LLM 调用，测量纯框架/网络延迟
    DEMO_MODE=false 时：真实 LLM 调用（会产生 API 费用！）
    """
    wait_time = between(1, 3)   # 每次请求后随机等待 1-3 秒

    @task(3)
    def chat_general(self):
        """通用旅游咨询"""
        payload = _random_chat_payload()
        with self.client.post(
            "/api/chat",
            json=payload,
            headers={"Accept": "text/event-stream"},
            stream=True,
            catch_response=True,
            name="/api/chat [general]",
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            stats = _consume_sse(resp)
            if stats["success"]:
                resp.success()
            else:
                resp.failure("SSE 流未收到 done 事件")

    @task(1)
    def chat_food_query(self):
        """美食专项查询（更高概率触发 AMAP 工具）"""
        city = random.choice(_CITIES)
        payload = {
            **_random_chat_payload(city),
            "message": f"{city}有哪些必吃的特色美食推荐",
        }
        with self.client.post(
            "/api/chat",
            json=payload,
            headers={"Accept": "text/event-stream"},
            stream=True,
            catch_response=True,
            name="/api/chat [food]",
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            stats = _consume_sse(resp)
            if stats["success"]:
                resp.success()
            else:
                resp.failure("SSE 流未收到 done 事件")


class OptimizeUser(HttpUser):
    """
    模拟排线优化用户

    纯算法（K-Means + TSP），无 LLM 调用（除 TipsGenerator 外）
    在 AMAP_MOCK=true + DEMO_MODE=true 下可完全离线运行
    """
    wait_time = between(2, 5)

    @task(2)
    def optimize_short_trip(self):
        """短途行程优化（3-4 个地点，2 天）"""
        payload = _random_optimize_payload(n_places=random.randint(3, 4))
        payload["trip_days"] = 2
        with self.client.post(
            "/api/optimize",
            json=payload,
            catch_response=True,
            name="/api/optimize [2days]",
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if data.get("itinerary"):
                    resp.success()
                else:
                    resp.failure("optimize 返回无 itinerary")
            else:
                resp.failure(f"HTTP {resp.status_code}: {resp.text[:100]}")

    @task(1)
    def optimize_long_trip(self):
        """长途行程优化（5-6 个地点，3-4 天）"""
        payload = _random_optimize_payload(n_places=random.randint(5, 6))
        payload["trip_days"] = random.randint(3, 4)
        with self.client.post(
            "/api/optimize",
            json=payload,
            catch_response=True,
            name="/api/optimize [3-4days]",
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if data.get("itinerary"):
                    resp.success()
                else:
                    resp.failure("optimize 返回无 itinerary")
            else:
                resp.failure(f"HTTP {resp.status_code}: {resp.text[:100]}")


class HealthUser(HttpUser):
    """健康探针基线（轻量，测量系统最小延迟）"""
    wait_time = between(0.5, 1.5)

    @task
    def health_check(self):
        self.client.get("/health", name="/health")

    @task
    def metrics_check(self):
        self.client.get("/metrics", name="/metrics")


# ═══════════════════════════════════════════════════════════════════
# 独立运行：无 locust 依赖的快速基准测试
# ═══════════════════════════════════════════════════════════════════

def run_quick_benchmark(host: str = "http://localhost:8000", n: int = 20) -> dict:
    """
    不依赖 locust 的轻量基准测试。
    使用 requests.Session（连接复用），统计 P50/P95/P99。

    用法：
      python backend/scripts/load_test.py --quick --host http://localhost:8000 --n 30
    """
    import statistics
    try:
        import requests
    except ImportError:
        print("请先安装：pip install requests")
        raise

    session = requests.Session()

    results: dict[str, list[float]] = {
        "/health": [],
        "/metrics": [],
        "/api/optimize": [],
    }

    print(f"\n快速基准测试（{n} 次/接口，host={host}，keep-alive 连接复用）...")
    print("注：首次请求会有连接建立开销，数据更可信\n")

    # 预热连接（首次 TCP 握手不计入统计）
    try:
        session.get(f"{host}/health", timeout=5)
    except Exception:
        pass

    # /health 和 /metrics（GET）
    for endpoint in ("/health", "/metrics"):
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                resp = session.get(f"{host}{endpoint}", timeout=5)
                resp.raise_for_status()
                results[endpoint].append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                print(f"  {endpoint} 请求失败：{e}")

    # /api/optimize（POST，K-Means+TSP 纯算法）
    for _ in range(n):
        payload = _random_optimize_payload(random.randint(3, 5))
        t0 = time.perf_counter()
        try:
            resp = session.post(
                f"{host}/api/optimize",
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            results["/api/optimize"].append((time.perf_counter() - t0) * 1000)
        except Exception as e:
            print(f"  /api/optimize 请求失败：{e}")

    # 打印统计
    print("\n=== 快速基准测试结果 ===")
    print(f"{'接口':<20} {'样本':>5} {'P50':>8} {'P95':>8} {'P99':>8} {'最大':>8}")
    print("-" * 55)
    summary = {}
    for endpoint, times in results.items():
        if not times:
            print(f"{endpoint:<20} {'N/A':>5}")
            continue
        sorted_t = sorted(times)
        p50 = statistics.median(sorted_t)
        p95 = sorted_t[int(len(sorted_t) * 0.95)]
        p99 = sorted_t[int(len(sorted_t) * 0.99)]
        mx = max(sorted_t)
        print(f"{endpoint:<20} {len(times):>5} {p50:>7.0f}ms {p95:>7.0f}ms "
              f"{p99:>7.0f}ms {mx:>7.0f}ms")
        summary[endpoint] = {"p50_ms": round(p50), "p95_ms": round(p95),
                              "p99_ms": round(p99), "max_ms": round(mx),
                              "samples": len(times)}
    print()
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BreezeTravel 压测工具")
    parser.add_argument("--quick", action="store_true",
                        help="快速基准（不需要 locust，直接发请求）")
    parser.add_argument("--host", default="http://localhost:8000",
                        help="目标 host")
    parser.add_argument("--n", type=int, default=20,
                        help="每接口请求次数（quick 模式）")
    parser.add_argument("--output", type=str, default=None,
                        help="保存结果到 JSON")
    args = parser.parse_args()

    if args.quick:
        result = run_quick_benchmark(host=args.host, n=args.n)
        if args.output:
            import pathlib
            pathlib.Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"结果已保存：{args.output}")
    else:
        print("请使用 locust 命令运行完整压测：")
        print("  locust -f backend/scripts/load_test.py --host http://localhost:8000")
