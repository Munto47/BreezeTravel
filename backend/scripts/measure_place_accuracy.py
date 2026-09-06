"""One bounded live pass over original public queries; never retain raw POIs or keys."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import logging
from pathlib import Path
import re
import time
import unicodedata

from dotenv import dotenv_values

from app.trip_understanding.amap_place import AmapPlaceResolver

ROOT = Path(__file__).resolve().parents[2]


def comparable(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    return re.sub(r'[\s()·—-]', '', value)


async def run(args):
    raw = args.cases.read_bytes()
    cases = json.loads(raw)['cases']
    values = dotenv_values(ROOT / '.local-artifacts/experience/experience.env', interpolate=False)
    resolver = AmapPlaceResolver(api_key=values.get('AMAP_API_KEY') or '')
    rows = []
    try:
        for index, case in enumerate(cases, 1):
            started = time.perf_counter()
            try:
                outcome = await resolver.resolve(city=case['city'], atomic_place_name=case['query'], category_hint=case['category'])
                matched = outcome.place.name if outcome.place else None
                correct = matched is not None and comparable(matched) in {comparable(name) for name in case['names']}
                verdict = 'CORRECT' if correct else 'WRONG' if matched else 'MISSED' if case['names'] else 'SAFE_REFUSAL'
                receipt = outcome.receipt
                row = {**case, 'verdict': verdict, 'matched': matched, 'status': receipt.get('status'),
                    'tier': receipt.get('selection_tier'), 'calls': receipt.get('external_calls', 0),
                    'candidate_count': receipt.get('provider_result_count'),
                    'type_conflicts': receipt.get('provider_type_conflict_candidate_count'),
                    'type_incomplete': receipt.get('provider_type_incomplete_candidate_count')}
            except Exception as error:
                row = {**case, 'verdict': 'ERROR', 'error_class': type(error).__name__,
                    'category': getattr(error, 'category', None), 'calls': getattr(error, 'external_call_count', None)}
            row['ms'] = round((time.perf_counter() - started) * 1000)
            rows.append(row)
            print(json.dumps({'progress': index, **row}, ensure_ascii=False), flush=True)
    finally:
        await resolver.aclose()
    summary = dict(Counter(row['verdict'] for row in rows))
    summary['cases'] = len(rows)
    summary['calls'] = sum(row['calls'] for row in rows) if all(type(row['calls']) is int for row in rows) else None
    report = {'cases_sha256': hashlib.sha256(raw).hexdigest(), 'summary': summary, 'observations': rows,
        'scope': 'Synthetic public-place queries; not human, blind or production evidence.', 'raw_responses_retained': False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'summary': summary}, ensure_ascii=False), flush=True)
    return int(summary.get('WRONG', 0) > 0 or summary.get('ERROR', 0) > 0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', type=Path, default=ROOT / 'backend/eval_data/place_accuracy_v1/cases.json')
    parser.add_argument('--output', type=Path, required=True)
    logging.disable(logging.CRITICAL)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
