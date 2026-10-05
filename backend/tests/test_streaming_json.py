import json
import pytest

from app.trip_understanding.streaming_json import StreamingObject


@pytest.mark.parametrize("width", [1, 2, 3, 7, 19, 1024])
def test_all_byte_boundaries_preserve_nested_chinese_and_escapes(width):
    value = {"activities": [{"name": '故宫\\"馆', "nested": [{"value": "😀\n园内"}]}], "count": 120, "done": True}
    data = json.dumps(value, ensure_ascii=False).encode()
    parser = StreamingObject()
    events = []
    for start in range(0, len(data), width):
        events.extend(parser.feed(data[start:start + width]))
    assert parser.finish() == value
    assert events == [("activities", 0, value["activities"][0]), ("count", None, 120), ("done", None, True)]


def test_complete_item_is_available_before_document_end():
    parser = StreamingObject()
    assert parser.feed('{"activities":[{"name":"故宫"}') == [("activities", 0, {"name": "故宫"})]
    assert parser.feed(',{"name":"未完') == []
    with pytest.raises(ValueError):
        parser.finish()


@pytest.mark.parametrize("data", [
    '{"activities":[{"role":"PLANNED","role":"EXCLUDED"}]}',
    '{"activities":[],"activities":[]}', '{"activities":[{}],}',
    '{"activities":[{},]}', '{"activities":[{}]} false',
    '{"value":NaN}', '{"value":12e}', '{"value":"\\uZZZZ"}',
])
def test_invalid_document_is_never_completed(data):
    parser = StreamingObject()
    with pytest.raises(ValueError):
        parser.feed(data)
        parser.finish()
