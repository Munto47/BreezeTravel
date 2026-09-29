"""Reuse still-current route facts by their actual input, across plan revisions."""
from app.trip_understanding.map_render import InternalRouteModeFact, ROUTE_CONFIG_SHA256
from app.trip_understanding.pipeline import canonical_sha256


def route_key(origin, destination, mode):
    return canonical_sha256({"config": ROUTE_CONFIG_SHA256, "mode": mode,
        "ends": [[stop.canonical_place_id, stop.longitude, stop.latitude, stop.city]
                 for stop in (origin, destination)]})


class ReusingRouteProvider:
    def __init__(self, provider, facts, *, reuse_only=False):
        self.provider = provider
        self.facts = facts
        self.reuse_only = reuse_only

    async def route(self, origin, destination, mode, *, observed_at):
        key = route_key(origin, destination, mode)
        fact = self.facts.get(key)
        if fact and fact.status == "AVAILABLE" and fact.expires_at > observed_at:
            return fact.model_copy(deep=True, update={"external_call_count": 0,
                "provider_binding": {**fact.provider_binding, "external_calls": 0, "reused": True}})
        if self.reuse_only:
            from datetime import timedelta
            return InternalRouteModeFact(mode=mode, status="UNAVAILABLE", request_hash=key,
                response_hash=key, provider_binding={"status":"NO_CURRENT_DATA", "external_calls":0},
                external_call_count=0, observed_at=observed_at, expires_at=observed_at+timedelta(minutes=1))
        fact = await self.provider.route(origin, destination, mode, observed_at=observed_at)
        fact.provider_binding["route_input_key"] = key
        self.facts[key] = fact.model_copy(deep=True)
        return fact


async def load_route_facts(repository, understanding_id, now):
    """Only the owning trip's persisted successful, unexpired observations."""
    if not hasattr(repository, '_get_pool'):
        return getattr(repository, '_reusable_route_facts', {}).get(understanding_id, {})
    pool = await repository._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT f.* FROM trip_map_route_mode_facts f
            JOIN trip_map_route_edges e ON e.edge_id=f.edge_id
            JOIN trip_map_render_snapshots s ON s.snapshot_id=e.snapshot_id
            JOIN trip_map_render_jobs j ON j.map_job_id=s.map_job_id
            WHERE j.understanding_id=$1 AND f.status='AVAILABLE' AND f.expires_at>$2
              AND f.provider_receipt_json ? 'route_input_key'
            ORDER BY f.observed_at DESC LIMIT 2000
        ''', understanding_id, now)
    from app.trip_understanding.map_repository import _json_value
    facts = {}
    for row in rows:
        binding = _json_value(row['provider_receipt_json'])
        key = binding['route_input_key']
        if key in facts:
            continue
        public, missing = await repository._mode_view_with_geometry({**dict(row),
            'provider_receipt_json': binding})
        if missing:
            continue
        facts[key] = InternalRouteModeFact(mode=row['mode'], status='AVAILABLE',
            duration_minutes=row['duration_minutes'], distance_meters=row['distance_meters'],
            transfer_count=row['transfer_count'], geometry=public.geometry,
            response_hash=row['response_hash'].strip(), request_hash=key,
            provider_binding=binding, external_call_count=0,
            observed_at=row['observed_at'], expires_at=row['expires_at'])
    return facts
