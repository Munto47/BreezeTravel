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
    LandmarkHint("北京", "北京大栅栏", ("前门大栅栏", "大栅栏"), "西城区", "061000", None,
                 "https://www.beijing.gov.cn/ywdt/zwzt/gjxxfxcs/gjf/xftyq/tyts/202404/t20240410_3615091.html"),
    LandmarkHint("上海", "万国建筑博览群", ("外滩万国建筑群", "外滩万国建筑", "外滩万国建筑博览群"), "黄浦区", "110200", None,
                 "https://www.shanghai.gov.cn/citywalk/20260625/9dfb6f86b9664e018607b5cc9ec9a53c.html"),
    LandmarkHint("上海", "武康大楼", (), "徐汇区", "120302", "商务住宅;住宅区;住宅小区",
                 "https://www.xuhui.gov.cn/zfjg_qzfbm_fgj_bmdt/20230128/509539.html"),
    LandmarkHint("上海", "思南公馆", (), "黄浦区", "120000", "商务住宅;商务住宅相关;商务住宅相关",
                 "https://whlyj.sh.gov.cn/cysc/20220228/25a0b870bdf34fa7a84e76d6edd2375c.html"),
    LandmarkHint("上海", "安福路", (), "徐汇区", "190301", "地名地址信息;交通地名;道路名",
                 "https://www.shanghai.gov.cn/citywalk/20260625/3492b1b945c146fc96c4585d7b04f00d.html"),
    LandmarkHint("北京", "中国美术馆", (), "东城区", "140100", "科教文化服务;博物馆;博物馆",
                 "https://www.namoc.org/zgmsg/cgfw/cgfw.shtml"),
    LandmarkHint("北京", "故宫博物院-神武门", ("故宫北门", "故宫博物院北门", "神武门"), "东城区", "110202", None,
                 "https://www.dpm.org.cn/explore/building/236456.html"),
    LandmarkHint("北京", "国家体育场", ("鸟巢", "国家体育场(鸟巢)"), "朝阳区", "080101", "体育休闲服务;运动场馆;综合体育馆",
                 "https://www.n-s.cn/aboutindex.html"),
    LandmarkHint("北京", "国家游泳中心", ("水立方", "国家游泳中心(水立方)"), "朝阳区", "080101", "体育休闲服务;运动场馆;综合体育馆",
                 "https://www.water-cube.com/"),
    LandmarkHint("北京", "后海", (), "西城区", "190205", "地名地址信息;自然地名;湖泊",
                 "https://swj.beijing.gov.cn/swdt/ztzl/hczzl/mtjj/201912/t20191219_1330573.html"),
    LandmarkHint("上海", "武康路", (), "徐汇区", "190301", "地名地址信息;交通地名;道路名",
                 "https://arabic.shanghai.gov.cn/cmsres/fb/fb4c67ebf9604322a8b3f6f74d7e1760/eec310a1555eb5a02a121cee2922800d.pdf"),
    LandmarkHint("上海", "豫园", (), "黄浦区", "110202", None,
                 "https://www.shanghai.gov.cn/citywalk/20260625/3a8ac74e088f4d468d9c3434b0b43d94.html"),
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
