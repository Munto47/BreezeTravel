"""Guide alternatives are bounded by explicit syntax, not recommendation words."""
import pytest

from app.trip_understanding.guide_choices import (
    choice_scopes,
    explicit_binary_choice_clauses,
    explicit_optional_labels,
    explicit_visit_labels,
)


def fragments(source):
    return [(source[start:end], day) for start, end, day in choice_scopes(source)]


def test_day_choice_requires_two_branches_and_stops_before_next_day():
    source = "## Day3｜二选一\n### 方案A：逛街\n**星河路**\n### 方案B：看展\n**江城博物馆**\n## Day4｜确定\n**澄湖公园**"
    assert fragments(source) == [(source[source.index("### 方案A"):source.index("## Day4")], 3)]


def test_chinese_day_and_version_headings_keep_all_names_uninterpreted():
    source = "## 第十二天：二选一\n### 版本Ａ：**栖云街、望星路**\n### 版本Ｂ：**江城美术馆**"
    assert fragments(source) == [(source[source.index("### 版本Ａ"):], 12)]


def test_only_one_branch_is_not_a_complete_unselected_choice():
    assert choice_scopes("## Day2 二选一\n### 方案A：**星河路**") == []


def test_two_version_headings_without_choice_title_do_not_make_planned_stops_optional():
    assert choice_scopes("## Day2 最终行程\n### 版本A：**星河路**\n### 版本B：**江城博物馆**") == []


def test_parenthesized_version_caption_is_a_branch_heading():
    source = "## Day3：城中 / 郊区二选一\n### 版本 A（休闲）\n云岭公园\n### 版本 B（登山）\n青溪山"
    assert fragments(source) == [(source[source.index("### 版本 A"):], 3)]


def test_explicit_conditional_replacement_quote_preserves_its_heading_day():
    source = "## Day2 长城\n**云岚长城**\n> 如果不想远行，替换方案：**星河博物馆**。\n\n晚上确定去**澄湖公园**。"
    quoted = "> 如果不想远行，替换方案：**星河博物馆**。\n"
    assert fragments(source) == [(quoted, 2)]


def test_labelled_backup_quote_can_cover_several_quoted_lines_only():
    source = "## Day1 爬山\n> 备选：如果下雨，把当天换成下面两站。\n> **星河博物馆、江城美术馆**\n\n**澄湖公园**是当天确定夜景。"
    start, end = source.index("> 备选"), source.index("\n\n") + 1
    assert fragments(source) == [(source[start:end], 1)]


def test_unscoped_backup_does_not_invent_a_day():
    source = "> 备选：如果下雨，可换成**星河博物馆**。"
    assert fragments(source) == [(source, None)]


def test_comparison_recommendation_and_negated_choice_do_not_trigger():
    source = "## Day2 不是二选一\n### 方案A：**星河路**\n### 方案B：**江城博物馆**\n> 星河路比望星路热闹，推荐逛街。\n> 如果不想远行，不用替换方案：仍按原计划。"
    assert choice_scopes(source) == []
    assert choice_scopes(source.replace("不是二选一", "不再是二选一")) == []


@pytest.mark.parametrize("correction", ["最终选定方案A。", "取消二选一，两处都去。"])
def test_later_selection_or_cancellation_disables_automatic_choice_scopes(correction):
    source = "## Day3 二选一\n### 方案A：**星河路**\n### 方案B：**江城博物馆**\n" + correction
    assert choice_scopes(source) == []
    assert choice_scopes(source.replace("最终选定方案A", "最终选A")) == []


def test_common_evening_and_conflicting_day_reference_do_not_get_a_guessed_day():
    source = "## Day2 二选一\n### 方案A：**星河路**\n### 方案B：**江城博物馆**\n无论选择哪一种，晚上都去澄湖公园。\n> 备选：如果天气差，把第3天换成**望星路**。"
    assert fragments(source) == [(source[source.index("> 备选"):], None)]
    ranged = "## Day2-3 二选一\n### 方案A：**星河路**\n### 方案B：**江城博物馆**"
    assert fragments(ranged) == [(ranged[ranged.index("### 方案A"):], None)]


def test_quoted_next_day_heading_cannot_enter_the_previous_backup_scope():
    source = "## Day2 爬山\n> 备选：如果下雨，换成**江城博物馆**。\n> ## Day3 确定主线\n> **澄湖公园**"
    assert fragments(source) == [(source[source.index("> 备选"):source.index("> ## Day3")], 2)]


@pytest.mark.parametrize("separator", ["，", ",", "；", ";", "/", "／"])
def test_binary_heading_keeps_two_complete_description_clauses_and_literal_day(separator):
    source = f"## Day2｜郊游（二选一： 云岭坡清静 {separator} 星河湾热闹 ）补充说明\n## Day3｜确定行程"
    clauses = explicit_binary_choice_clauses(source)
    assert [(source[left:right], day) for left, right, day in clauses] == [
        ("云岭坡清静", 2), ("星河湾热闹", 2),
    ]
    assert all(source[left:right] in {"云岭坡清静", "星河湾热闹"} for left, right, _ in clauses)


def test_binary_heading_without_parentheses_stops_at_line_end_and_accepts_chinese_day():
    source = "第十二天 二选一：**云岭坡**清静，**星河湾**热闹\n这段正文不属于选择标题。"
    assert [(source[left:right], day) for left, right, day in explicit_binary_choice_clauses(source)] == [
        ("**云岭坡**清静", 12), ("**星河湾**热闹", 12),
    ]


@pytest.mark.parametrize("source", [
    "Day2 二选一：方案A清静，方案B热闹",
    "Day2 二选一：版本 A 清静，版本 B 热闹",
    "Day2 二选一：云岭坡清静，星河湾热闹。\n已选择云岭坡。",
    "Day2 二选一：云岭坡清静，星河湾热闹。\n最终采用前者。",
    "Day2 取消二选一：云岭坡清静，星河湾热闹",
    "Day2 二选一：云岭坡清静，星河湾热闹。\n更正：当天两处都去。",
    "> ## Day2 二选一：云岭坡清静，星河湾热闹",
    "引用：\n## Day2 二选一：云岭坡清静，星河湾热闹",
    "```text\n## Day2 二选一：云岭坡清静，星河湾热闹\n```",
    "“\n## Day2 二选一：云岭坡清静，星河湾热闹\n”",
    "介绍里写到 Day2 二选一：云岭坡清静，星河湾热闹",
    "Day2 三选一：云岭坡清静，星河湾热闹，青溪岛较远",
    "Day2 二选一：云岭坡清静，星河湾热闹，青溪岛较远",
    "Day2 二选一：，星河湾热闹",
    "Day2 二选一：云岭坡清静， ",
    "Day2 二选一：云岭坡清静",
    "Day2 二选一：云岭坡（南段）清静，星河湾热闹",
    "Day2（二选一：云岭坡清静，星河湾热闹",
    "Day2–3 二选一：云岭坡清静，星河湾热闹",
    "Day2至3 二选一：云岭坡清静，星河湾热闹",
    "Day0 二选一：云岭坡清静，星河湾热闹",
    "Day2 明天二选一：云岭坡清静，星河湾热闹",
    "Day2 二选一：https://example.test，星河湾热闹",
])
def test_binary_heading_rejects_decided_quoted_nested_or_ambiguous_clauses(source):
    assert explicit_binary_choice_clauses(source) == []


@pytest.mark.parametrize("source,name", [
    ("时间充裕可以去云岭古镇逛街。", "云岭古镇"),
    ("下午有空可去望星屯散步。", "望星屯"),
    ("星河咖啡可以打卡特调。", "星河咖啡"),
    ("闲逛拍照，XYZ 咖啡可以打卡特调。", "XYZ 咖啡"),
    ("傍晚：青溪会址可顺路参观；之后返回。", "青溪会址"),
    ("时间充裕可以去**云岭古镇**逛街。", "云岭古镇"),
])
def test_explicit_optional_noun_offsets_preserve_only_the_literal_name(source, name):
    assert explicit_optional_labels(source) == [(source.index(name), source.index(name) + len(name))]


@pytest.mark.parametrize("source", [
    "不要觉得可以去云岭古镇，这次取消了。",
    "云岭古镇可以打卡，不过这次不去。",
    "引用：可以去云岭古镇逛街。",
    "资料写道：“星河咖啡可以打卡特调”。",
    "> 如果有空，可以去云岭古镇逛街。",
    "可以去https://example.test/云岭古镇。",
    "可去中山路18号公园打卡。",
    "如果想喝咖啡可以打卡特调。",
    "冰美式咖啡可以打卡特调。",
    "推荐炸酱面和云香烤鸭，味道不错。",
    "可以去云岭古镇和青溪公园。",
    "云岭古镇可以打卡吗？",
    "先到星河咖啡，可以打卡特调。",
])
def test_optional_noun_check_does_not_promote_negation_description_dishes_or_references(source):
    assert explicit_optional_labels(source) == []


def test_optional_noun_check_returns_separate_source_spans_without_cross_sentence_joining():
    source = "第1天先去澄湖公园。时间充裕可以去云岭古镇逛街。第2天：青溪会址可顺路参观。"
    assert [source[start:end] for start, end in explicit_optional_labels(source)] == ["云岭古镇", "青溪会址"]


@pytest.mark.parametrize("source,expected", [
    ("中午：云岭路 / 星河路吃本地面、简餐", ["云岭路", "星河路"]),
    ("- 午餐：**青溪街**／**望星胡同**用餐。", ["青溪街", "望星胡同"]),
    ("• 晚餐：云岭步行街 / 星河大街吃饭", ["云岭步行街", "星河大街"]),
    ("Day1\r\n- 中午：云岭路 / 星河路用餐。\r\n晚上休息。", ["云岭路", "星河路"]),
])
def test_meal_street_slash_choice_preserves_two_atomic_original_labels(source, expected):
    spans = explicit_optional_labels(source)
    assert [source[left:right] for left, right in spans] == expected
    assert spans == [(source.index(name), source.index(name) + len(name)) for name in expected]


@pytest.mark.parametrize("source", [
    "中午：云岭路 / 星河路吃饭，依次前往。",
    "中午：云岭路 / 星河路吃饭，按顺序走。",
    "中午：云岭路 / 星河路吃饭，两个都去。",
    "中午：云岭路 / 星河路吃饭，分别吃面和点心。",
    "中午：云岭路 / 星河路吃饭，这两条路都逛。",
    "午餐：云岭路 / 星河路用餐，两处均用餐。",
    "中午：云岭路 / 星河路吃饭，但这项取消了。",
    "引用：\n中午：云岭路 / 星河路吃饭。",
    "> 中午：云岭路 / 星河路吃饭。",
    "```text\n中午：云岭路 / 星河路吃饭。\n```",
    "中午：https://example.test/云岭路 / 星河路吃饭。",
    "上午：云岭路 / 星河路吃饭。",
    "路线：云岭路 / 星河路吃饭。",
    "中午：步行到云岭路 / 星河路吃饭。",
    "中午：云岭路 / 星河路吃饭，导航途经两条路。",
    "中午：云岭路 / 星河路看展。",
    "中午：云岭路 / 星河路 / 青溪路吃饭。",
    "中午：云岭路 / 星河路吃饭 / 青溪路喝茶。",
    "中午：云岭路18号 / 星河路吃饭。",
    "中午：当地小路 / 一家餐厅吃饭。",
])
def test_meal_street_choice_rejects_both_visits_negation_references_and_routes(source):
    assert explicit_optional_labels(source) == []


@pytest.mark.parametrize("source,name", [
    ("如果喜欢幻想主题：直接把 Day3 替换云岚梦境一整天，门票提前确认。", "云岚梦境"),
    ("可以将 D2 替换为星河乐园全天。", "星河乐园"),
    ("可选：把第3天替换成**云岚梦境**一整天。", "云岚梦境"),
    ("如果下雨，可以把 Day2 替换为M28艺境全天。", "M28艺境"),
])
def test_whole_day_alternative_keeps_exact_literal_name_without_requiring_a_venue_suffix(source, name):
    assert explicit_optional_labels(source) == [(source.index(name), source.index(name) + len(name))]


def test_whole_day_replacement_anchors_the_replacement_not_a_repeated_intro_name():
    source = "### 如果喜欢云岚梦境：直接把 Day3 替换云岚梦境一整天，提前查交通。"
    start = source.rindex("云岚梦境")
    assert explicit_optional_labels(source) == [(start, start + len("云岚梦境"))]


def test_direct_whole_day_replacement_requires_an_unselected_scope_and_stops_at_next_day():
    source = (
        "## Day3 二选一\n### 方案A：城中\n星河坊\n### 方案B：郊外\n青溪滩\n"
        "直接把 Day3 替换云岚梦境一整天。\n"
        "## Day4 确定行程\n直接把 Day4 替换望星幻境一整天。"
    )
    assert [source[start:end] for start, end in explicit_optional_labels(source)] == ["云岚梦境"]


@pytest.mark.parametrize("source", [
    "把 Day3 替换云岚梦境一整天。",
    "直接把 Day3 替换云岚梦境一整天。",
    "已决定采用：可以把 Day3 替换云岚梦境一整天。",
    "可以把 Day3 替换云岚梦境一整天。\n已选定该替代方案。",
    "不要把 Day3 替换云岚梦境一整天，即使有人说可以。",
    "如果有空，把 Day3 替换云岚梦境一整天，不过这项已经取消。",
    "如果有空，把 Day3 替换云岚梦境一整天。\n取消上述替换方案。",
    "引用：如果喜欢主题游，把 Day3 替换云岚梦境一整天。",
    "“可以把 Day3 替换云岚梦境一整天。”",
    "> 如果喜欢主题游，把 Day3 替换云岚梦境一整天。",
    "https://example.test/如果/把Day3替换云岚梦境一整天",
    "可以把 Day3 替换云岭路18号一整天。",
    "可以把 Day3 替换云岚梦境和望星幻境一整天。",
    "可以把 Day3 替换去云岚梦境一整天。",
    "可以把 Day3 替换自由活动一整天。",
    "可以把 Day3 替换云岚梦境一整天的介绍发给同伴。",
])
def test_whole_day_alternative_rejects_decisions_negation_references_and_non_atomic_text(source):
    assert explicit_optional_labels(source) == []


def test_settled_day_choice_does_not_force_a_direct_whole_day_replacement_optional():
    source = (
        "## Day3 二选一\n### 方案A：城中\n星河坊\n### 方案B：郊外\n青溪滩\n"
        "直接把 Day3 替换云岚梦境一整天。\n最终选定方案A。"
    )
    assert explicit_optional_labels(source) == []


@pytest.mark.parametrize("source,name", [
    ("拍照以后，顺着云岭路慢慢走。", "云岭路"),
    ("星河坊简单逛一圈即可，接着吃饭。", "星河坊"),
    ("参观结束；星河坊简单逛一圈即可。", "星河坊"),
    ("傍晚青溪滩看落日，之后吃晚饭。", "青溪滩"),
    ("傍晚：**青溪滩**看落日。", "青溪滩"),
])
def test_closed_visit_grammar_preserves_exact_noun_offsets(source, name):
    assert explicit_visit_labels(source) == [(source.index(name), source.index(name) + len(name))]


@pytest.mark.parametrize("source", [
    "如果有空，顺着云岭路慢慢走。",
    "时间充裕可以顺着云岭路慢慢走。",
    "备选：星河坊简单逛一圈即可。",
    "取消这项，星河坊简单逛一圈即可。",
    "傍晚青溪滩看落日，不过这次不去了。",
    "引用：顺着云岭路慢慢走。",
    "作者写道：“傍晚青溪滩看落日”。",
    "> 顺着云岭路慢慢走。",
    "去年顺着云岭路慢慢走。",
    "傍晚美丽海滩看落日。",
    "星河坊适合简单逛一圈。",
    "顺着云岭路18号慢慢走。",
    "傍晚青溪滩看落日吗？",
    "顺着云岭路慢慢走的介绍很详细。",
    "小王顺着云岭路慢慢走，这是小说中的一幕。",
    "傍晚青溪滩看落日的照片挂在展厅。",
])
def test_closed_visit_grammar_rejects_conditional_reference_and_descriptive_text(source):
    assert explicit_visit_labels(source) == []


def test_explicit_visit_in_a_whole_day_choice_still_has_a_separate_optional_scope():
    source = "## Day3 二选一\n### 方案A：城中\n星河坊简单逛一圈即可。\n### 方案B：海边\n傍晚青溪滩看落日。\n## Day4\n顺着云岭路慢慢走。"
    labels = explicit_visit_labels(source)
    assert [source[start:end] for start, end in labels] == ["星河坊", "青溪滩", "云岭路"]
    scopes = choice_scopes(source)
    assert len(scopes) == 1
    left, right, day = scopes[0]
    assert day == 3
    assert all(left <= start < end <= right for start, end in labels[:2])
    assert labels[2][0] >= right
