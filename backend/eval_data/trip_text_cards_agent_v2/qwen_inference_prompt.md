# G01 structured trip understanding

Return exactly one object conforming to the supplied JSON Schema. Do not add
Markdown or commentary.

Destination:

- Use `EXPLICIT` only when the destination city name occurs verbatim in the
  source outside a POI name. `evidence_span_start/end` must select exactly that
  city token.
- A trip may name more than one destination. In that case preserve the exact,
  contiguous source expression (for example `北京、杭州`) and its exact span.
  Never translate, romanize, normalize or reorder an explicit destination.
- Otherwise use `SOFT_ASSUMPTION`; both evidence fields must be null or omitted.
  Do not turn a guessed city into source evidence.

Mentions:

- Emit one mention for every occurrence of a place entity outside a URL,
  including repeated occurrences in reference, optional, pass-through and
  exclusion sentences. The mention span is the place name only, never the full
  activity clause. Do not emit separate mentions for descriptions, transport
  instructions, reservation notes or URLs.
  Offsets are Unicode code-point, zero-based, half-open.
- `atomic_place_name` may be non-null only when the selected span, after outer
  whitespace is removed, is exactly that standalone place name.
- A whole sentence, description, route instruction, booking note, phone number
  or URL is never an atomic place. When the exact span clearly is a place entity,
  `atomic_place_name` MUST equal that span; do not set it to null. Use null only
  when the source itself does not provide a reliable place-name boundary. Never
  invent or normalize a name.
- Classify every place mention from its local sentence as exactly one of:
  - `PLANNED`: the author has committed to visit or stop there;
  - `OPTIONAL`: it is a backup, time-permitting choice, or may be skipped;
  - `REFERENCE`: somebody mentioned or recommended it, or the text says it is
    not part of the current arrangement without explicitly rejecting it;
  - `EXCLUDED`: the author explicitly says not to go, cancel, remove or exclude;
  - `PASS_THROUGH`: the itinerary only passes through or transfers there.
  Phrases such as “听说/网友提到/另一篇攻略/不是本次安排” are `REFERENCE`,
  not `EXCLUDED`. A quoted example inside an instruction is not a substitute
  for the later, actual local intent sentence.
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
