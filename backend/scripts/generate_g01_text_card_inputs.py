from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from evals.trip_text_cards_v1.contracts import (
    AdjudicationBundle,
    AnnotationBundle,
    RuntimeGateEvidence,
    TextCardInputCase,
    TextCardPrediction,
    sha256_text,
)


GENERATOR_ID = "g01-text-card-input-generator-v1"


@dataclass(frozen=True)
class FamilySpec:
    family_id: str
    split: str
    cohort: str
    city_scope: tuple[str, ...]
    profile: str
    places: tuple[str, str, str, str, str, str]
    optional_place: str
    excluded_place: str
    pass_through_place: str
    reference_place: str


FAMILIES = (
    # dev: 12 deep-city families, 3 other-city families, 3 adversarial families.
    FamilySpec("G01-F001", "dev", "DEEP_CITY", ("北京",), "中轴线与老城慢行", ("故宫博物院", "景山公园", "天坛公园", "前门大街", "北海公园", "恭王府"), "南锣鼓巷", "北京环球影城", "王府井地铁站", "中国国家博物馆"),
    FamilySpec("G01-F002", "dev", "DEEP_CITY", ("北京",), "西北园林与校园周边", ("颐和园", "圆明园", "北京大学", "清华大学艺术博物馆", "香山公园", "国家植物园"), "北京动物园", "八达岭长城", "西苑地铁站", "中央电视塔"),
    FamilySpec("G01-F003", "dev", "DEEP_CITY", ("北京",), "胡同、鼓楼与社区街区", ("雍和宫", "国子监", "孔庙", "钟鼓楼", "什刹海", "烟袋斜街"), "五道营胡同", "欢乐谷", "鼓楼大街地铁站", "簋街"),
    FamilySpec("G01-F004", "dev", "DEEP_CITY", ("北京",), "奥运场馆与当代艺术", ("奥林匹克森林公园", "国家体育场", "国家游泳中心", "中国科学技术馆", "798艺术区", "红砖美术馆"), "中国电影博物馆", "古北水镇", "奥林匹克公园地铁站", "中央美术学院美术馆"),
    FamilySpec("G01-F005", "dev", "DEEP_CITY", ("上海",), "外滩与老城公共空间", ("外滩", "豫园", "上海博物馆", "人民广场", "南京东路步行街", "苏州河步道"), "新天地", "上海迪士尼乐园", "南京东路地铁站", "东方明珠广播电视塔"),
    FamilySpec("G01-F006", "dev", "DEEP_CITY", ("上海",), "法租界街区与美术馆", ("武康大楼", "上海图书馆东馆", "徐家汇书院", "龙华寺", "西岸美术馆", "上海当代艺术博物馆"), "田子坊", "朱家角古镇", "常熟路地铁站", "上海交响音乐厅"),
    FamilySpec("G01-F007", "dev", "DEEP_CITY", ("上海",), "浦东天际线与亲子场馆", ("陆家嘴中心绿地", "上海海洋水族馆", "上海科技馆", "世纪公园", "浦东美术馆", "滨江大道"), "上海天文馆", "七宝古镇", "陆家嘴地铁站", "上海中心大厦"),
    FamilySpec("G01-F008", "dev", "DEEP_CITY", ("上海",), "虹口历史建筑与滨水", ("鲁迅公园", "上海鲁迅纪念馆", "多伦路文化名人街", "北外滩国客中心", "白玉兰广场", "和平公园"), "1933老场坊", "滴水湖", "四川北路地铁站", "上海邮政博物馆"),
    FamilySpec("G01-F009", "dev", "DEEP_CITY", ("杭州",), "西湖东线与湖滨步行", ("断桥残雪", "白堤", "孤山", "曲院风荷", "苏堤", "雷峰塔"), "浙江省博物馆", "宋城", "龙翔桥地铁站", "三潭印月"),
    FamilySpec("G01-F010", "dev", "DEEP_CITY", ("杭州",), "灵隐与茶园山径", ("灵隐寺", "飞来峰", "永福寺", "法喜寺", "梅家坞", "龙井村"), "九溪烟树", "杭州乐园", "灵隐公交中心站", "中国茶叶博物馆"),
    FamilySpec("G01-F011", "dev", "DEEP_CITY", ("杭州",), "运河文化与城市博物馆", ("拱宸桥", "中国京杭大运河博物馆", "桥西历史文化街区", "小河直街", "香积寺", "武林门码头"), "大兜路历史街区", "千岛湖", "拱宸桥东地铁站", "杭州工艺美术博物馆"),
    FamilySpec("G01-F012", "dev", "DEEP_CITY", ("杭州",), "湿地、良渚与郊野空间", ("西溪湿地国家公园", "良渚博物院", "良渚古城遗址公园", "梦想小镇", "杭州植物园", "太子湾公园"), "湘湖国家旅游度假区", "瑶琳仙境", "良渚地铁站", "浙江自然博物院"),
    FamilySpec("G01-F013", "dev", "OTHER_CITY", ("成都",), "成都老城与公园茶馆", ("武侯祠", "锦里古街", "人民公园", "宽窄巷子", "成都博物馆", "东郊记忆"), "大慈寺", "都江堰景区", "天府广场地铁站", "成都大熊猫繁育研究基地"),
    FamilySpec("G01-F014", "dev", "OTHER_CITY", ("西安",), "城墙内外与考古场馆", ("西安城墙", "碑林博物馆", "钟楼", "鼓楼", "陕西历史博物馆", "大雁塔"), "大唐不夜城", "华山", "永宁门地铁站", "秦始皇帝陵博物院"),
    FamilySpec("G01-F015", "dev", "OTHER_CITY", ("广州",), "岭南建筑与珠江两岸", ("陈家祠", "永庆坊", "沙面岛", "广东省博物馆", "花城广场", "广州塔"), "越秀公园", "长隆欢乐世界", "黄沙地铁站", "白云山风景区"),
    FamilySpec("G01-F016", "dev", "ADVERSARIAL", ("北京", "上海"), "跨城市攻略夹杂比较和否定", ("故宫博物院", "景山公园", "天坛公园", "外滩", "豫园", "上海博物馆"), "东方明珠广播电视塔", "北京环球影城", "虹桥火车站", "颐和园"),
    FamilySpec("G01-F017", "dev", "ADVERSARIAL", ("杭州",), "网址、预约句和地点描述混排", ("西湖风景名胜区", "灵隐寺", "雷峰塔", "河坊街", "西溪湿地国家公园", "中国丝绸博物馆"), "龙井村", "宋城", "凤起路地铁站", "岳王庙"),
    FamilySpec("G01-F018", "dev", "ADVERSARIAL", ("上海",), "酒店餐厅同名与多重角色", ("外滩", "上海博物馆", "豫园", "田子坊", "新天地", "世纪公园"), "全季酒店（南京东路店）", "上海迪士尼乐园", "人民广场地铁站", "南翔馒头店（豫园店）"),
    # validation: labels must later come from two authorized humans.
    FamilySpec("G01-F019", "validation", "DEEP_CITY", ("北京",), "皇家坛庙与城市公园", ("天坛公园", "先农坛", "陶然亭公园", "永定门公园", "北京古代建筑博物馆", "龙潭公园"), "日坛公园", "北京野生动物园", "天桥地铁站", "自然博物馆"),
    FamilySpec("G01-F020", "validation", "DEEP_CITY", ("北京",), "长城近郊与十三陵", ("八达岭长城", "居庸关长城", "明十三陵", "定陵博物馆", "昌平公园", "中国航空博物馆"), "慕田峪长城", "北京环球影城", "清河站", "银山塔林"),
    FamilySpec("G01-F021", "validation", "DEEP_CITY", ("上海",), "杨浦工业遗产与滨江", ("杨浦滨江", "上海国际时尚中心", "复兴岛公园", "共青森林公园", "上海体育博物馆", "江湾体育场"), "上海犹太难民纪念馆", "上海迪士尼乐园", "杨树浦路地铁站", "上海自来水科技馆"),
    FamilySpec("G01-F022", "validation", "DEEP_CITY", ("杭州",), "南宋遗址与凤凰山麓", ("南宋德寿宫遗址博物馆", "胡雪岩故居", "南宋御街", "鼓楼", "万松书院", "八卦田遗址公园"), "玉皇山", "杭州野生动物世界", "定安路地铁站", "中国美术学院美术馆"),
    FamilySpec("G01-F023", "validation", "OTHER_CITY", ("南京",), "民国建筑与城墙轴线", ("中山陵", "明孝陵", "南京博物院", "总统府", "玄武湖公园", "南京城墙"), "老门东", "牛首山文化旅游区", "新街口地铁站", "侵华日军南京大屠杀遇难同胞纪念馆"),
    FamilySpec("G01-F024", "validation", "ADVERSARIAL", ("北京", "杭州"), "同名地点、整句描述与中途换城", ("北京鼓楼", "什刹海", "北京西站", "杭州鼓楼", "南宋御街", "西湖风景名胜区"), "河坊街", "北京环球影城", "杭州东站", "鼓楼"),
    # frozen blind inputs are visible; labels remain outside the repository with an independent custodian.
    FamilySpec("G01-F025", "frozen_blind", "DEEP_CITY", ("北京",), "博物馆群与城市公共文化", ("首都博物馆", "中国美术馆", "北京天文馆", "中国地质博物馆", "北京石刻艺术博物馆", "中国园林博物馆"), "中国铁道博物馆", "欢乐谷", "木樨地地铁站", "国家典籍博物馆"),
    FamilySpec("G01-F026", "frozen_blind", "DEEP_CITY", ("上海",), "古镇水乡与郊区博物馆", ("七宝古镇", "闵行博物馆", "上海地铁博物馆", "召稼楼古镇", "浦江郊野公园", "上海奇迹花园"), "新场古镇", "上海迪士尼乐园", "莘庄地铁站", "上海航宇科普中心"),
    FamilySpec("G01-F027", "frozen_blind", "DEEP_CITY", ("上海",), "静安寺周边与城市展馆", ("静安寺", "上海自然博物馆", "四行仓库抗战纪念馆", "苏河湾万象天地", "静安雕塑公园", "上海展览中心"), "上海历史博物馆", "上海欢乐谷", "汉中路地铁站", "上海美术电影制片厂"),
    FamilySpec("G01-F028", "frozen_blind", "DEEP_CITY", ("杭州",), "钱塘江南岸与城市阳台", ("杭州城市阳台", "钱江新城灯光秀观景区", "杭州博物馆", "白马湖公园", "湘湖国家旅游度假区", "跨湖桥遗址博物馆"), "钱塘江博物馆", "杭州乐园", "江陵路地铁站", "浙江省科技馆"),
    FamilySpec("G01-F029", "frozen_blind", "OTHER_CITY", ("苏州",), "园林、古城与运河街巷", ("拙政园", "苏州博物馆", "狮子林", "平江路", "虎丘", "山塘街"), "留园", "华谊兄弟电影世界", "北寺塔地铁站", "网师园"),
    FamilySpec("G01-F030", "frozen_blind", "ADVERSARIAL", ("上海", "杭州"), "高铁换城、经过站点、链接与备选混杂", ("外滩", "豫园", "虹桥火车站", "西湖风景名胜区", "灵隐寺", "杭州东站"), "河坊街", "上海迪士尼乐园", "嘉兴南站", "东方明珠广播电视塔"),
)


def _text_for(spec: FamilySpec, variant: str) -> str:
    p = spec.places
    cities = "、".join(spec.city_scope)
    url = f"https://example.invalid/g01/{spec.family_id.lower()}/{variant.lower()}?place={p[0]}"
    common_end = (
        f"{spec.reference_place}只是从另一篇攻略里听说的参考项，不表示已经安排；"
        f"{spec.optional_place}仅在时间充裕时作为备选；行程途中会经过{spec.pass_through_place}，但不在那里游览；"
        f"这次明确不去{spec.excluded_place}。预约说明写着“热门地点可能需要提前确认”，详情链接为{url}，"
        "说明句和网址都不是地点。文中日期仍未确定，人数也没有最终确认，先按普通三日草稿理解；"
        "任何营业、票价和开放信息都需要临行前另行核验，不能把这段描述当作实时事实。"
    )
    if variant == "A":
        return (
            f"这是一份围绕{cities}的三天长攻略草稿，主题是{spec.profile}。作者把明确安排、别人的推荐和否定项写在同一段里，"
            "请按原意拆成每天的地点，不要把说明性整句当作地点。"
            f"第1天上午先到{p[0]}，午后步行到{p[1]}，两处都属于当天确定计划。"
            f"第2天安排{p[2]}和{p[3]}，顺序以正文为准，不自动补充附近景点。"
            f"第3天先去{p[4]}，再到{p[5]}结束当天。"
            + common_end
        )
    if variant == "B":
        return (
            f"朋友转来一段{cities}的{spec.profile}笔记，原文很长且混有交通提醒。没有填写日历日期，也没说最终同行人数。"
            f"Day 1 确定游览{p[1]}，随后前往{p[0]}；Day 2 上午看{p[3]}，下午安排{p[2]}；"
            f"Day 3 把{p[5]}放在前面，再去{p[4]}。这些六处才是逐日计划。"
            f"不要因为文字里出现“{spec.reference_place}很有名”就自动加入，也不要把“去{spec.excluded_place}”从否定句里截出来。"
            + common_end
        )
    return (
        f"整理{cities}的{spec.profile}路线时，原作者用了自然段而不是表格，还夹着网址、预约提示和中转站。"
        f"第一天的确定行程是{p[0]}与{p[2]}；第二天先{p[1]}后{p[4]}；第三天依次到{p[3]}、{p[5]}。"
        f"原文补充说：如果当天太累，{spec.optional_place}可以完全不去；只是路过{spec.pass_through_place}换乘；"
        f"网友曾提到{spec.reference_place}，但这不是本次安排；已经决定排除{spec.excluded_place}。"
        f"可查看{url}了解预约流程，不过网址和“预约流程请提前核对”这句话不能生成地点卡。"
        "这份文本没有可靠的价格、开放时间或路线耗时，日期和人数均需保留为可编辑软假设，不能自行编造。"
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(output_root: Path) -> dict[str, object]:
    output_root.mkdir(parents=True, exist_ok=True)
    by_split: dict[str, list[dict[str, object]]] = {"dev": [], "validation": [], "frozen_blind": []}
    case_number = 1
    for spec in FAMILIES:
        parent_case_id = f"G01-TC-{case_number:03d}"
        for variant in ("A", "B", "C"):
            case_id = f"G01-TC-{case_number:03d}"
            text = _text_for(spec, variant)
            case = TextCardInputCase(
                case_id=case_id,
                family_id=spec.family_id,
                variant_id=variant,
                split=spec.split,
                cohort=spec.cohort,
                city_scope=list(spec.city_scope),
                input_text=text,
                normalized_input_sha256=sha256_text(text),
                lineage={
                    "data_origin": "HIGH_FIDELITY_SYNTHETIC",
                    "generator_id": GENERATOR_ID,
                    "template_family_id": f"g01-template-{spec.family_id.casefold().replace('g01-', '')}",
                    "mutation_parent_case_id": None if variant == "A" else parent_case_id,
                },
            )
            by_split[spec.split].append(case.model_dump(mode="json"))
            case_number += 1

    paths = {
        "dev.inputs.jsonl": by_split["dev"],
        "validation.inputs.jsonl": by_split["validation"],
        "frozen_blind.inputs.jsonl": by_split["frozen_blind"],
    }
    for filename, values in paths.items():
        _write_jsonl(output_root / filename, values)

    schemas = {
        "input.schema.json": TextCardInputCase.model_json_schema(),
        "annotation.schema.json": AnnotationBundle.model_json_schema(),
        "adjudication.schema.json": AdjudicationBundle.model_json_schema(),
        "prediction.schema.json": TextCardPrediction.model_json_schema(),
        "runtime_evidence.schema.json": RuntimeGateEvidence.model_json_schema(),
    }
    for filename, schema in schemas.items():
        _write_json(output_root / filename, schema)

    generator_sha256 = _sha256(Path(__file__))
    receipt = {
        "schema_version": "g01-text-card-generation-receipt-v1",
        "dataset_version": "g01-text-card-dataset-v1",
        "generator_id": GENERATOR_ID,
        "generator_sha256": generator_sha256,
        "data_origin": "HIGH_FIDELITY_SYNTHETIC",
        "generated_at": "2026-08-28T00:00:00+08:00",
        "case_count": 90,
        "family_count": 30,
        "split_counts": {split: len(values) for split, values in by_split.items()},
        "cohort_counts": {"DEEP_CITY": 60, "OTHER_CITY": 15, "ADVERSARIAL": 15},
        "human_annotations_created": 0,
        "provider_calls": 0,
        "historical_dataset_inputs_used": 0,
    }
    _write_json(output_root / "generation_receipt.json", receipt)

    bound_files = [*paths, *schemas, "generation_receipt.json"]
    contract = {
        "schema_version": "g01-text-card-dataset-contract-v1",
        "dataset_version": "g01-text-card-dataset-v1",
        "case_count": 90,
        "family_count": 30,
        "required_split_counts": {"dev": 54, "validation": 18, "frozen_blind": 18},
        "required_cohort_counts": {"DEEP_CITY": 60, "OTHER_CITY": 15, "ADVERSARIAL": 15},
        "generator_sha256": generator_sha256,
        "files": {filename: _sha256(output_root / filename) for filename in sorted(bound_files)},
        "truth_custody": {
            "dev_validation": "DUAL_HUMAN_ANNOTATION_REQUIRED",
            "frozen_blind": "EXTERNAL_CUSTODIAN_ONLY",
            "repository_truth_payloads": 0,
            "sealed_blind_runs_consumed": 0,
        },
        "gate_claim": "NOT_RUN",
    }
    _write_json(output_root / "dataset_contract.json", contract)
    return contract


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("eval_data/trip_text_cards_v1"),
    )
    args = parser.parse_args()
    print(json.dumps(generate(args.output_root), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
