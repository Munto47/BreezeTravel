# Open-community route priors

`RoutePriorLoader` is a read-only, hash-verifying loader for the minimal
Wikivoyage projections archived in `backend/eval_data/dual_entry_v1/archives`.
It does not write a `SuggestionSet`, accept candidates, or record user events.

The only permitted contributions are:

- content relevance;
- diversity;
- weak route adjacency.

Every returned `PriorCandidateHint` is an unresolved text query and carries the
fixed revision, content hash, attribution and licence. Before it can become a
candidate place, the configured Provider must resolve canonical identity,
city and coordinates and issue its own receipt.

Community priors never establish current opening hours, reservation status,
price, accessibility, route duration or popularity. An article-cluster order
is not a geographic order. No RecommendationEvent, acceptance rate or other
live-user signal is inferred from Wikivoyage content.

Current pinned pages:

- Beijing revision `5331911`;
- Shanghai revision `5306138`;
- Hangzhou revision `5265453`;
- reuse policy revision `4999830`.

## Official route priors

`signals_for_city(city, anchor_query)` keeps the two provenance classes
separate: `community_hints` remain attributed CC BY-SA material, while
`official_hints` carry `official_prior_refs` bound to the registry row, raw
archive SHA-256, extract SHA-256 and captured remote-body SHA-256.

An official route contributes only an unresolved query string and the fact
that it was immediately adjacent to the anchor in that fixed publication.
It does not establish a canonical POI, coordinates, current opening or booking
state, route time, accessibility, price or popularity. Every hint still has
`requires_provider_resolution=true`.

Official availability is explicit rather than inferred from an empty result:

- Beijing: verified archive available;
- Shanghai: the archived Citywalk source is available; the unarchived vote
  page remains listed in `unavailable_source_ids` and contributes nothing;
- Hangzhou: `OFFICIAL_ARCHIVE_UNAVAILABLE`; its unavailable PDF record produces
  neither an official hint nor an official reference. Community content is
  never promoted to official provenance.

The loader fails closed if the canonical URL, raw/extract file hash, derivation
remote-body hash, minimal route projection, `STRUCTURE`/`EVAL_ONLY` boundary or
the archived prohibited-claim declaration is altered.

The derived extracts remain attributed to Wikivoyage contributors under
CC BY-SA 4.0. Images are not part of this data chain.
