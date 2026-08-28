# G01 structured trip understanding

Return exactly one object conforming to the supplied JSON Schema. Do not add
Markdown or commentary.

Destination:

- Use `EXPLICIT` only when the destination city name occurs verbatim in the
  source outside a POI name. `evidence_span_start/end` must select exactly that
  city token.
- Otherwise use `SOFT_ASSUMPTION`; both evidence fields must be null or omitted.
  Do not turn a guessed city into source evidence.

Mentions:

- Emit one mention for each place entity used by an activity. The mention span
  is the place name only, never the full activity clause. Do not emit separate
  mentions for descriptions, transport instructions, reservation notes or URLs.
  Offsets are Unicode code-point, zero-based, half-open.
- `atomic_place_name` may be non-null only when the selected span, after outer
  whitespace is removed, is exactly that standalone place name.
- A whole sentence, description, route instruction, booking note, phone number
  or URL is never an atomic place. When the exact span clearly is a place entity,
  `atomic_place_name` MUST equal that span; do not set it to null. Use null only
  when the source itself does not provide a reliable place-name boundary. Never
  invent or normalize a name.
- Classify every place mention as exactly one of `PLANNED`, `OPTIONAL`,
  `REFERENCE`, `EXCLUDED`, or `PASS_THROUGH` from the author's intent.
- Every `PLANNED` mention must have a day index. Use explicit Day/第N天 structure;
  if an intended stop has no written day, conservatively assign Day 1. Other
  roles may use null when no day is explicitly associated.
- `sequence_index` starts at 0 and preserves source order within each day/role
  group. `category_hint` and `time_hint` are hints from the source, not verified
  Provider facts.

Do not claim that any POI, city, category, route, opening time or booking fact is
verified. Deterministic application code performs Provider resolution later.

Example rule: in “Day 1 上午去故宫博物院，预约说明见链接”, emit only the exact
“故宫博物院” span as the place mention and set `atomic_place_name` to the same
text. Do not emit “预约说明见链接” as a place mention.
