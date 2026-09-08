"""Known noun checks must not ask users to add an explicit reference as a visit."""
import pytest
from app.trip_understanding.semantic_recovery import explicit_reference_context

@pytest.mark.parametrize("source,name,expected", [
    ("上万春亭俯瞰故宫全景，日落拍照绝美。", "故宫", "VIEWED_OBJECT"),
    ("上高台眺望星河公园全景，拍照留念。", "星河公园", "VIEWED_OBJECT"),
    ("景山公园，神武门过马路即到，上万春亭。", "神武门", "DIRECTION_ORIGIN"),
    ("目的地青溪公园，南门过马路即到。", "南门", "DIRECTION_ORIGIN"),
    ("Day2：游览故宫，俯瞰全景。", "故宫", None),
    ("上高台俯瞰故宫全景，随后进入参观。", "故宫", None),
    ("前往神武门，然后过马路去景山公园。", "神武门", None),
    ("神武门参观后过马路即到。", "神武门", None),
    ("抵达南门过马路即到集合点。", "南门", None),
    ("从高台看星河公园后继续游览。", "星河公园", None),
    ("故宫是今天的主线。", "故宫", None),
])
def test_only_explicit_reference_is_not_an_additional_visit(source, name, expected):
    start=source.index(name)
    assert explicit_reference_context(source,start,start+len(name))==expected

def test_reference_cannot_waive_later_real_visit_to_same_name():
    source="上万春亭俯瞰故宫全景。Day2：参观故宫。"
    first=source.index("故宫")
    second=source.index("故宫",first+2)
    assert explicit_reference_context(source,first,first+2)=="VIEWED_OBJECT"
    assert explicit_reference_context(source,second,second+2) is None
