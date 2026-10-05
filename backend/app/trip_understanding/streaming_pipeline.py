"""Project cumulative semantics using the ordinary compiler and result rules."""
from __future__ import annotations

import asyncio
import copy


class StableCompiler:
    def __init__(self, compiler):
        self.compiler = compiler
        self.identities = {}

    def compile(self, source, proposal):
        activities, claims, receipt = self.compiler.compile(source, proposal)
        remap = {}
        stable = []
        for activity in activities:
            mention = activity.mention
            # Source occurrence, not result position or POI identity. Distinct
            # names in a shared quote remain separate occurrences.
            key = (mention.span_start, mention.span_end, mention.raw_text)
            identifier, token = self.identities.setdefault(key, (activity.activity_id, activity.public_activity_token))
            remap[activity.activity_id] = identifier
            stable.append(activity.model_copy(update={"activity_id": identifier, "public_activity_token": token}))
        return stable, [claim.model_copy(update={"activity_id": remap[claim.activity_id]}) for claim in claims], receipt


class SharedPlaceQueries:
    def __init__(self, resolver, concurrency):
        self.resolver = resolver
        self.slots = asyncio.Semaphore(concurrency)
        self.tasks = {}

    async def resolve(self, **query):
        key = tuple(sorted(query.items()))
        if key not in self.tasks:
            async def run():
                async with self.slots:
                    return await self.resolver.resolve(**query)
            self.tasks[key] = asyncio.create_task(run())
        # An obsolete semantic projection cannot cancel a query still needed
        # by its successor. Failures are retained for this task, not retried.
        return await asyncio.shield(self.tasks[key])

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


async def run_streaming(pipeline, source, **options):
    from app.trip_understanding.streaming_semantics import propose_stream
    child = copy.copy(pipeline)
    child.compiler = StableCompiler(pipeline.compiler)
    queries = SharedPlaceQueries(pipeline.place_resolver, pipeline.max_place_concurrency)
    child.place_resolver = queries
    callback = options.pop("progress_callback", None)
    generation = 0
    projection = None
    latest_snapshot = None
    latest_places = {}
    pending = None
    writer = None
    first_confirmed = False
    last_write = 0.0
    write_lock = asyncio.Lock()

    async def flush():
        nonlocal pending, last_write
        async with write_lock:
            if pending is not None:
                update, pending = pending, None
                await callback(update)
                last_write = asyncio.get_running_loop().time()

    async def delayed_flush():
        await asyncio.sleep(max(0, .25 - (asyncio.get_running_loop().time() - last_write)))
        await flush()

    async def enqueue(update):
        nonlocal pending, writer, first_confirmed
        if writer is not None and writer.done():
            writer.result()
        confirmed = any(card.status == "READY" for day in update.snapshot.days for card in day.activities)
        immediate = last_write == 0 or confirmed and not first_confirmed
        pending = update
        first_confirmed = first_confirmed or confirmed
        if immediate:
            await flush()
        elif writer is None or writer.done():
            writer = asyncio.create_task(delayed_flush())

    async def on_plan(plan, semantic_complete):
        nonlocal generation, projection, latest_snapshot, pending
        generation += 1
        pending = None
        current = generation
        if projection is not None:
            if projection.done():
                projection.result()  # Never hide a persistence or program error.
            else:
                projection.cancel()
                try:
                    await projection
                except asyncio.CancelledError:
                    pass

        async def publish(update):
            nonlocal latest_snapshot, latest_places
            if current != generation:
                return
            snapshot = update.snapshot
            places = dict(update.internal_binding.get("preview_places", {}))
            # Recompilation must not briefly demote unchanged confirmed cards
            # while the same completed lookup is retrieved from the task cache.
            if update.phase == "CARDS_AVAILABLE" and latest_snapshot is not None:
                ready = {(day.label, card.activity_token, card.name, card.city): card
                         for day in latest_snapshot.days for card in day.activities if card.status == "READY"}
                snapshot = snapshot.model_copy(update={"days": [day.model_copy(update={"activities": [
                    ready.get((day.label, card.activity_token, card.name, card.city), card)
                    if card.status != "READY" else card for card in day.activities]}) for day in snapshot.days]})
                for day in snapshot.days:
                    for card in day.activities:
                        if card.status == "READY" and card.activity_token in latest_places:
                            places.setdefault(card.activity_token, latest_places[card.activity_token])
            latest_snapshot = snapshot
            latest_places = places
            if callback:
                await enqueue(update.model_copy(update={"snapshot": snapshot,
                    "internal_binding": {**update.internal_binding, "preview_places": places},
                    "progress": update.progress.model_copy(update={"semantic_complete": semantic_complete,
                        "places_total_final": semantic_complete})}))

        projection = asyncio.create_task(child.run(source, prepared_plan=plan, progress_callback=publish,
                                                   progressive_places=True, **options))
        # Give newly closed items a chance to dispatch before the next network
        # chunk. IO remains concurrent with the model response.
        await asyncio.sleep(0)

    try:
        await propose_stream(pipeline.inference_provider, source, on_plan)
        result = await projection
        if writer is not None:
            await writer
        if callback:
            await flush()
        return result
    finally:
        if projection is not None and not projection.done():
            projection.cancel()
            await asyncio.gather(projection, return_exceptions=True)
        if writer is not None and not writer.done():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        await queries.close()
