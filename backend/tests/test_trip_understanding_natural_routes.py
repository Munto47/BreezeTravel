from __future__ import annotations

import re

import pytest

from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.models import ActivityRole, ProposedMention
from app.trip_understanding.pipeline import is_atomic_planned_place


YUNTAISHAN_TEXT = """建议行程 [编辑]
经典两日游路线为：第一天游览泉瀑峡、潭瀑峡、猕猴谷及万善寺；
第二天一早前往红石峡（避开人潮高峰），后续可游子房湖及茱萸峰
（经叠彩洞往返）。节假日游客众多，红石峡通道较窄，有条件者建议
错峰出行以避开排队人流。"""

ZHANGJIAJIE_TEXT = """话，可以参考如下线路：
• 第一天，早上由张家界永定市区的中心汽车站乘车前往森林公园门
票站。在旅游旺季，由于多数游客选择这个入口，一般早上八九点
入口处就已是人山人海了。买票进入，步行一段平路约6分钟，来
到大氧吧广场，这里是许多旅行团和游客集中的地方，也是入园后
的第一个风景点。这时可以选择在广场西侧（左侧）的车站乘坐环
保车约4分钟到黄石寨游览，也可以直接前往金鞭溪或鹞子寨。上
黄石寨一般是坐索道（在下车的地方旁边坐，索道站可以拿黄石寨
地图）到寨顶（寨顶较为平坦），七分钟后到达寨顶。寨顶的游览
线路是环形的，出站向左向右皆可。绕整个寨顶一圈大约100分
钟，基本为平路，偶有起伏。黄石寨东侧的山谷对面是袁家界，下
方山谷是金鞭溪，北面远处是杨家界南端。黄石寨游客不少，以六
奇阁游客最多。若选择步行下山，有前山后山两条路，后山下山约
1小时，到达索道下站；前山下山是多数人选择的线路，下山约需1
小时，一路下台阶直达大氧吧广场。大氧吧广场附近有一些商亭卖
东西，还有餐厅。之后便可以继续游览金鞭溪或鹞子寨。多数人选
择前者，因为后者需要爬山，很远，而且较陡，适合登山爱好者。
金鞭溪游览线4km多，全程很是惬意，旁边的溪水很清，可以到水
边体验一下。走完一般是第一天下午四点多钟，走到后一半有一个"""

QINLING_TEXT = """时较长，但是乐趣无穷。
• 都督门 太白庙 大坪 灵官台 老庙子
  • 早晨到达都督门，开始三天的旅程。这一天要从都督门走到老
庙子。
  • 海拔高度：都督门(1704M)-太白庙(1996M)-大坪(2482M)-灵官
台(2908M)-老庙子(3034M)。
  • 穿越时间：约10小时
  • 当晚营地：老庙子(有水源)
  • 穿越距离：约15公里，上升为主
  • 累计爬升：约1330米
• 老庙子 将军庙 莲花石 万仙阵 灵官庙 跑马梁 拔仙台 大爷海 大文公
庙 放羊寺
  • 休息一晚继续第二天的旅途。
  • 海拔高度：老庙子(3034M)-将军庙(3321M)-莲花石(3379M)-万
仙阵(3562M)-灵官庙(也称雷公庙3537M)-跑马梁(约3500M左
右)-拔仙台(3769M)-大爷海(3596M)-大文公庙(3454M)-放羊寺
(3072M)
  • 穿越时间：10-12小时"""

YUNTAISHAN_OCR_TEXT = """建议行程[编辑]
经典两日游路线为：第一天游览泉瀑峡、潭瀑峡、猕猴谷及万善寺：第二天一早前往红石峡（避开人潮高峰），后续可游览子房湖及茱萸峰（经叠
彩洞往返）。节假日游客众多，红石峡通道较窄，有条件者建议错峰出行以避开排队人流。"""

QINLING_OCR_TEXT = """•都督门太白庙大坪灵官台老庙子
● 早晨到达都督门，开始三天的旅程。这一天要从都督门走到老庙子。
●海拔高度：都督门(1704M)-太白庙(1996M)-大坪(2482M)-灵官台(2908M)-老庙子(3034M)。
●穿越时间：约10小时
●当晚营地：老庙子(有水源）
•穿越距离：约15公里，上升为主
●累计爬升：约1330米"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_text", "expected"),
    [
        (
            YUNTAISHAN_TEXT,
            [
                ("泉瀑峡", "PLANNED", 1),
                ("潭瀑峡", "PLANNED", 1),
                ("猕猴谷", "PLANNED", 1),
                ("万善寺", "PLANNED", 1),
                ("红石峡", "PLANNED", 2),
                ("子房湖", "OPTIONAL", None),
                ("茱萸峰", "OPTIONAL", None),
                ("叠彩洞", "PASS_THROUGH", None),
            ],
        ),
        (
            ZHANGJIAJIE_TEXT,
            [
                ("张家界永定市区的中心汽车站", "PASS_THROUGH", None),
                ("森林公园门票站", "PLANNED", 1),
                ("大氧吧广场", "PLANNED", 1),
                ("黄石寨", "OPTIONAL", None),
                ("金鞭溪", "OPTIONAL", None),
                ("鹞子寨", "OPTIONAL", None),
            ],
        ),
        (
            QINLING_TEXT,
            [
                ("都督门", "PLANNED", 1),
                ("太白庙", "PLANNED", 1),
                ("大坪", "PLANNED", 1),
                ("灵官台", "PLANNED", 1),
                ("老庙子", "PLANNED", 1),
                ("老庙子", "PLANNED", 2),
                ("将军庙", "PLANNED", 2),
                ("莲花石", "PLANNED", 2),
                ("万仙阵", "PLANNED", 2),
                ("灵官庙", "PLANNED", 2),
                ("跑马梁", "PLANNED", 2),
                ("拔仙台", "PLANNED", 2),
                ("大爷海", "PLANNED", 2),
                ("大文公庙", "PLANNED", 2),
                ("放羊寺", "PLANNED", 2),
            ],
        ),
    ],
)
async def test_natural_itinerary_roles_days_and_atomic_names_are_exact(
    source_text: str,
    expected: list[tuple[str, str, int | None]],
) -> None:
    output = await build_full_text_pipeline().run(source_text)
    mentions = [item.compiled.mention for item in output.activities]

    assert [
        (item.atomic_place_name, item.role.value, item.day_index)
        for item in mentions
    ] == expected
    assert output.compiler_receipt == {
        "compiler": "trip-understanding-evidence-compiler-v1",
        "unicode_basis": "CODE_POINT_HALF_OPEN",
        "mention_count": len(expected),
        "valid_span_count": len(expected),
        "eligible_place_count": sum(
            item.compiled.eligible_for_place_search for item in output.activities
        ),
    }
    for mention in mentions:
        assert source_text[mention.span_start : mention.span_end] == mention.raw_text
        assert re.sub(r"\s+", "", mention.raw_text) == mention.atomic_place_name


@pytest.mark.asyncio
async def test_natural_itinerary_does_not_promote_descriptions_aliases_or_fused_places() -> None:
    outputs = [
        await build_full_text_pipeline().run(source_text)
        for source_text in (YUNTAISHAN_TEXT, ZHANGJIAJIE_TEXT, QINLING_TEXT)
    ]
    atomic_names = {
        item.compiled.mention.atomic_place_name
        for output in outputs
        for item in output.activities
    }

    assert "红石峡（避开人潮高峰）" not in atomic_names
    assert "金鞭溪或鹞子寨" not in atomic_names
    assert "雷公庙" not in atomic_names
    assert "的旅途" not in atomic_names
    assert all("http://" not in (item or "") and "https://" not in (item or "") for item in atomic_names)


@pytest.mark.asyncio
async def test_short_user_authored_lines_remain_separate_source_items() -> None:
    source_text = "Day 1\n故宫博物院\n景山公园"

    output = await build_full_text_pipeline().run(source_text)

    assert [
        item.compiled.mention.atomic_place_name for item in output.activities
    ] == ["故宫博物院", "景山公园"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_text", "expected_planned"),
    [
        (
            YUNTAISHAN_OCR_TEXT,
            ["泉瀑峡", "潭瀑峡", "猕猴谷", "万善寺", "红石峡"],
        ),
        (
            QINLING_OCR_TEXT,
            ["都督门", "太白庙", "大坪", "灵官台", "老庙子"],
        ),
    ],
)
async def test_real_ocr_route_punctuation_and_collapsed_title_spacing_are_supported(
    source_text: str,
    expected_planned: list[str],
) -> None:
    output = await build_full_text_pipeline().run(source_text)

    assert [
        item.compiled.mention.atomic_place_name
        for item in output.activities
        if item.compiled.mention.role == ActivityRole.PLANNED
    ] == expected_planned


@pytest.mark.asyncio
async def test_standalone_elevation_reference_is_not_promoted_to_a_plan() -> None:
    output = await build_full_text_pipeline().run(
        "资料摘录：海拔高度：甲峰(1704M)-乙峰(1996M)。"
    )

    assert all(
        item.compiled.mention.role != ActivityRole.PLANNED
        for item in output.activities
    )


def test_atomic_place_allows_only_visual_newline_repair_not_ordinary_space_fusion() -> None:
    wrapped = ProposedMention(
        mention_id="wrapped",
        raw_text="森林公园门\n票站",
        span_start=0,
        span_end=8,
        role=ActivityRole.PLANNED,
        day_index=1,
        sequence_index=0,
        atomic_place_name="森林公园门票站",
        category_hint="交通节点",
    )
    fused = wrapped.model_copy(
        update={
            "mention_id": "fused",
            "raw_text": "故宫 天坛",
            "span_end": 5,
            "atomic_place_name": "故宫天坛",
        }
    )

    assert is_atomic_planned_place(wrapped) is True
    assert is_atomic_planned_place(fused) is False
