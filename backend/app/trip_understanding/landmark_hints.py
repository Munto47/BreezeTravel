"""Reviewed search hints, never POI identities, coordinates or live facts.

Small additions to the frozen 900-place lexicon. A hint still needs an exact
provider name, city/district, category and unique POI result at request time.
"""
from dataclasses import dataclass

from app.trip_understanding._three_city_place_lexicon import normalize_city_name, normalize_place_name


@dataclass(frozen=True)
class LandmarkHint:
    city: str
    name: str
    aliases: tuple[str, ...]
    district: str
    typecode: str
    technical_label: str | None
    source: str

    def matches(self, name: str) -> bool:
        normalized = normalize_place_name(name)
        names = {normalize_place_name(value) for value in (self.name, *self.aliases)}
        return normalized in names or any(normalized == normalize_place_name(prefix + value)
            for prefix in (self.city, self.city + "市") for value in (self.name, *self.aliases))


HINTS = (
    LandmarkHint("北京", "国家体育场", ("鸟巢", "国家体育场(鸟巢)"), "朝阳区", "080101", "体育休闲服务;运动场馆;综合体育馆",
                 "https://www.n-s.cn/aboutindex.html"),
    LandmarkHint("北京", "国家游泳中心", ("水立方", "国家游泳中心(水立方)"), "朝阳区", "080101", "体育休闲服务;运动场馆;综合体育馆",
                 "https://www.water-cube.com/"),
    LandmarkHint("北京", "后海", (), "西城区", "190205", "地名地址信息;自然地名;湖泊",
                 "https://swj.beijing.gov.cn/swdt/ztzl/hczzl/mtjj/201912/t20191219_1330573.html"),
    LandmarkHint("上海", "武康路", (), "徐汇区", "190301", "地名地址信息;交通地名;道路名",
                 "https://arabic.shanghai.gov.cn/cmsres/fb/fb4c67ebf9604322a8b3f6f74d7e1760/eec310a1555eb5a02a121cee2922800d.pdf"),
    LandmarkHint("上海", "东方明珠广播电视塔", ("东方明珠", "东方明珠电视塔"), "浦东新区", "110202", None,
                 "https://www.meet-in-shanghai.net/tc/news/shanghai-municipal-administration-of-culture-and-tourism-will-launch-the-five-hearts-plan-to-revitalize-the-cultural-tourism-market-and-many-scenic-spots-will-reopen-part-i-200655/"),
)


def landmark_hint(city: str, name: str) -> LandmarkHint | None:
    matches = [hint for hint in HINTS if hint.city == normalize_city_name(city) and hint.matches(name)]
    return matches[0] if len(matches) == 1 else None


def verified_technical_landmark(row: dict, *, city: str, name: str) -> bool:
    hint = landmark_hint(city, name)
    return bool(hint and hint.technical_label and hint.matches(str(row.get("name") or ""))
                and row.get("typecode") == hint.typecode and row.get("type") == hint.technical_label
                and row.get("adname") == hint.district)
