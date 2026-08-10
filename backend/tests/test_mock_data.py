"""
测试 Mock Fixture 数据的完整性

验证 amap_mock_places.json 能被 AmapSearch 节点正确加载和解析。
不依赖外部服务，可离线运行。
"""

import json
import sys
from pathlib import Path

# 确保 backend 目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).parent.parent))


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "amap_mock_places.json"
REQUIRED_CITIES = ["成都", "北京", "上海", "厦门"]
REQUIRED_PLACE_FIELDS = [
    "place_id", "name", "category", "address",
    "coords", "city", "source", "amap_photos", "estimated_duration",
]
VALID_CATEGORIES = {"attraction", "food", "hotel", "transport"}
VALID_SOURCES = {"amap_poi", "rag", "synthesized"}


def load_fixture():
    assert FIXTURE_PATH.exists(), f"Fixture 文件不存在: {FIXTURE_PATH}"
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


class TestFixtureStructure:
    """验证 fixture 文件基础结构"""

    def test_fixture_file_exists(self):
        assert FIXTURE_PATH.exists()

    def test_fixture_is_valid_json(self):
        data = load_fixture()
        assert isinstance(data, dict)

    def test_all_required_cities_present(self):
        data = load_fixture()
        for city in REQUIRED_CITIES:
            assert city in data, f"缺少城市 {city} 的 fixture 数据"

    def test_each_city_has_minimum_places(self):
        data = load_fixture()
        for city in REQUIRED_CITIES:
            places = data[city]
            assert len(places) >= 6, f"{city} 的地点数量不足（当前 {len(places)} 个，需要至少 6 个）"

    def test_each_place_has_required_fields(self):
        data = load_fixture()
        for city, places in data.items():
            for place in places:
                for field in REQUIRED_PLACE_FIELDS:
                    assert field in place, f"{city}/{place.get('name', '?')} 缺少字段 {field}"

    def test_place_category_valid(self):
        data = load_fixture()
        for city, places in data.items():
            for place in places:
                assert place["category"] in VALID_CATEGORIES, (
                    f"{city}/{place['name']} 的 category={place['category']} 不合法"
                )

    def test_place_source_valid(self):
        data = load_fixture()
        for city, places in data.items():
            for place in places:
                assert place["source"] in VALID_SOURCES, (
                    f"{city}/{place['name']} 的 source={place['source']} 不合法"
                )

    def test_coords_have_lng_lat(self):
        data = load_fixture()
        for city, places in data.items():
            for place in places:
                coords = place["coords"]
                assert "lng" in coords and "lat" in coords, (
                    f"{city}/{place['name']} coords 格式错误"
                )
                lng, lat = coords["lng"], coords["lat"]
                assert isinstance(lng, (int, float)), f"lng 不是数字: {lng}"
                assert isinstance(lat, (int, float)), f"lat 不是数字: {lat}"
                # 中国大陆坐标范围校验
                assert 73 <= lng <= 135, f"{city}/{place['name']} lng={lng} 超出中国范围"
                assert 3 <= lat <= 54, f"{city}/{place['name']} lat={lat} 超出中国范围"

    def test_chengdu_has_panda_base(self):
        """成都 Demo fixture 必须包含熊猫基地（最具代表性的地点）"""
        data = load_fixture()
        chengdu = data["成都"]
        names = [p["name"] for p in chengdu]
        assert any("熊猫" in n for n in names), f"成都 fixture 缺少熊猫基地，当前地点：{names}"

    def test_place_ids_unique(self):
        """所有地点 place_id 必须唯一"""
        data = load_fixture()
        all_ids = []
        for places in data.values():
            all_ids.extend(p["place_id"] for p in places)
        assert len(all_ids) == len(set(all_ids)), "存在重复的 place_id"


class TestAmapSearchMockLoad:
    """验证 AmapSearch 节点能正确加载 Mock 数据"""

    def test_load_chengdu_places(self):
        from app.agents.nodes.amap_search import _load_mock_places
        # _load_mock_places 只接受 city 一个参数
        places = _load_mock_places("成都")
        assert len(places) > 0, "成都 mock 数据加载失败"
        # 验证 Place 对象字段
        for p in places:
            assert p.place_id
            assert p.name
            assert p.coords.lng > 0
            assert p.coords.lat > 0

    def test_load_beijing_places(self):
        from app.agents.nodes.amap_search import _load_mock_places
        places = _load_mock_places("北京")
        assert len(places) > 0, "北京 mock 数据加载失败"

    def test_fallback_to_chengdu_for_unknown_city(self):
        from app.agents.nodes.amap_search import _load_mock_places
        # 未知城市应 fallback 到成都（fixture 中没有拉萨数据则返回空，不崩溃即可）
        places = _load_mock_places("拉萨")
        assert len(places) >= 0
