from types import SimpleNamespace

from app.trip_understanding.memory_share import build_share_projection


def test_share_omits_unresolved_places_without_mutating_stored_days():
    def activity(name, status):
        return SimpleNamespace(
            name=name, status=status, area_or_address="合成地址", time_hint=None
        )

    resolved = activity("合成公园", "READY")
    unresolved = activity("找不到的地点", "NEEDS_CONFIRMATION")
    result = SimpleNamespace(
        assumptions=[], stay=SimpleNamespace(candidates=[]),
        days=[
            SimpleNamespace(label="Day 1", activities=[unresolved, resolved]),
            SimpleNamespace(label="Day 2", activities=[unresolved]),
        ],
    )

    shared = build_share_projection(result)
    assert [item.name for item in shared.days[0].activities] == ["合成公园"]
    assert shared.days[0].activities[0].note == "可直接查看"
    assert shared.days[1].activities == []
    assert result.days[0].activities == [unresolved, resolved]
    assert result.days[1].activities == [unresolved]
