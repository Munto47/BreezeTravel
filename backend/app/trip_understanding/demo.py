from __future__ import annotations

import hashlib

from app.trip_understanding.models import (
    ActivityRole,
    InferenceProposal,
    ProposedMention,
    ResolvedPlace,
)


DEMO_SOURCE_TEXT = """北京三日慢游，不设具体日历日期。
Day 1 上午先去故宫博物院，故宫需要提前预约，说明见 https://ticket.dpm.org.cn/；下午登景山公园看中轴线。
Day 2 上午逛天坛公园，下午去前门大街散步。
Day 3 上午游览颐和园，下午到圆明园。
如果体力允许，南锣鼓巷只作为备选；这次明确不安排北京环球影城。"""
DEMO_SOURCE_SHA256 = "864dd50d49c38f92cf78e33abf2bf03fc86e23c6f14977919e6c4f16a64f1222"
if hashlib.sha256(DEMO_SOURCE_TEXT.encode("utf-8")).hexdigest() != DEMO_SOURCE_SHA256:
    raise RuntimeError("fixed Beijing demo source hash drifted")


def _mention(
    text: str,
    raw_text: str,
    *,
    mention_id: str,
    role: ActivityRole,
    day_index: int | None,
    sequence_index: int,
    atomic_place_name: str | None = None,
    category_hint: str | None = None,
    time_hint: str | None = None,
) -> ProposedMention:
    start = text.index(raw_text)
    return ProposedMention(
        mention_id=mention_id,
        raw_text=raw_text,
        span_start=start,
        span_end=start + len(raw_text),
        role=role,
        day_index=day_index,
        sequence_index=sequence_index,
        atomic_place_name=atomic_place_name,
        category_hint=category_hint,
        time_hint=time_hint,
    )


class FixedBeijingDemoInferenceProvider:
    """Frozen fixture implementation of the model-neutral provider contract."""

    async def propose(self, source_text: str) -> InferenceProposal:
        if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != DEMO_SOURCE_SHA256:
            raise ValueError("fixed demo source binding mismatch")
        mentions = [
            _mention(
                source_text,
                "故宫博物院",
                mention_id="demo-m01",
                role=ActivityRole.PLANNED,
                day_index=1,
                sequence_index=0,
                atomic_place_name="故宫博物院",
                category_hint="文化古迹",
                time_hint="上午",
            ),
            _mention(
                source_text,
                "故宫需要提前预约",
                mention_id="demo-m02",
                role=ActivityRole.REFERENCE,
                day_index=1,
                sequence_index=1,
            ),
            _mention(
                source_text,
                "https://ticket.dpm.org.cn/",
                mention_id="demo-m03",
                role=ActivityRole.PASS_THROUGH,
                day_index=1,
                sequence_index=2,
            ),
            _mention(
                source_text,
                "景山公园",
                mention_id="demo-m04",
                role=ActivityRole.PLANNED,
                day_index=1,
                sequence_index=3,
                atomic_place_name="景山公园",
                category_hint="公园",
                time_hint="下午",
            ),
            _mention(
                source_text,
                "天坛公园",
                mention_id="demo-m05",
                role=ActivityRole.PLANNED,
                day_index=2,
                sequence_index=0,
                atomic_place_name="天坛公园",
                category_hint="公园",
                time_hint="上午",
            ),
            _mention(
                source_text,
                "前门大街",
                mention_id="demo-m06",
                role=ActivityRole.PLANNED,
                day_index=2,
                sequence_index=1,
                atomic_place_name="前门大街",
                category_hint="街区",
                time_hint="下午",
            ),
            _mention(
                source_text,
                "颐和园",
                mention_id="demo-m07",
                role=ActivityRole.PLANNED,
                day_index=3,
                sequence_index=0,
                atomic_place_name="颐和园",
                category_hint="公园",
                time_hint="上午",
            ),
            _mention(
                source_text,
                "圆明园",
                mention_id="demo-m08",
                role=ActivityRole.PLANNED,
                day_index=3,
                sequence_index=1,
                atomic_place_name="圆明园",
                category_hint="公园",
                time_hint="下午",
            ),
            _mention(
                source_text,
                "南锣鼓巷",
                mention_id="demo-m09",
                role=ActivityRole.OPTIONAL,
                day_index=None,
                sequence_index=0,
                atomic_place_name="南锣鼓巷",
                category_hint="街区",
            ),
            _mention(
                source_text,
                "北京环球影城",
                mention_id="demo-m10",
                role=ActivityRole.EXCLUDED,
                day_index=None,
                sequence_index=1,
                atomic_place_name="北京环球影城",
                category_hint="主题乐园",
            ),
        ]
        return InferenceProposal(
            source_hash=DEMO_SOURCE_SHA256,
            destination_name="北京",
            mentions=mentions,
            binding={
                "contract": "StructuredInferenceProvider/v1",
                "provider": "fixed_fixture",
                "snapshot": "beijing-demo-2026-08-27-v1",
                "external_calls": 0,
            },
        )


class FixedBeijingPlaceResolver:
    _PLACES = {
        "故宫博物院": ("fixture-bj-palace-museum", "文化古迹", "东城区·景山前街4号", 116.3913, 39.9163),
        "景山公园": ("fixture-bj-jingshan", "公园", "东城区·景山西街44号", 116.3974, 39.9254),
        "天坛公园": ("fixture-bj-temple-of-heaven", "公园", "东城区·天坛路甲1号", 116.4071, 39.8822),
        "前门大街": ("fixture-bj-qianmen", "街区", "东城区·前门大街", 116.3936, 39.8992),
        "颐和园": ("fixture-bj-summer-palace", "公园", "海淀区·新建宫门路19号", 116.2755, 39.9999),
        "圆明园": ("fixture-bj-old-summer-palace", "公园", "海淀区·清华西路28号", 116.3039, 40.0081),
    }

    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
    ) -> ResolvedPlace | None:
        del category_hint
        if city != "北京":
            return None
        value = self._PLACES.get(atomic_place_name)
        if value is None:
            return None
        canonical_place_id, category, address, longitude, latitude = value
        return ResolvedPlace(
            canonical_place_id=canonical_place_id,
            name=atomic_place_name,
            category=category,
            area_or_address=address,
            provider_binding={
                "provider": "frozen_beijing_fixture",
                "snapshot": "beijing-poi-demo-2026-08-27-v1",
                "external_calls": 0,
                "coordinates": {
                    "longitude": longitude,
                    "latitude": latitude,
                },
            },
        )


def build_demo_pipeline():
    from app.trip_understanding.pipeline import TripUnderstandingPipeline

    return TripUnderstandingPipeline(
        inference_provider=FixedBeijingDemoInferenceProvider(),
        place_resolver=FixedBeijingPlaceResolver(),
    )
