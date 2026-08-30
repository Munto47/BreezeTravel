# G01 structured trip understanding

Return exactly one object conforming to the supplied JSON Schema. Do not add
Markdown or commentary.

Destination:

- Use `EXPLICIT` only when the destination city name occurs verbatim in the
  source outside a POI name. `evidence_span_start/end` must select exactly that
  city token. Do not return a separate name for `EXPLICIT`; application code
  copies it from that exact span.
- A trip may name more than one destination. In that case preserve the exact,
  contiguous source expression (for example `北京、杭州`) and its exact span.
  Never translate, romanize, normalize or reorder an explicit destination.
- Otherwise use `SOFT_ASSUMPTION`; both evidence fields must be null or omitted.
  Do not turn a guessed city into source evidence.

Mentions:

- Emit one mention for each place occurrence that states the author's actual
  trip intent: a committed stop, conditional option, outside reference,
  explicit exclusion or pass-through. The mention span is the place name only,
  never the full activity clause.
- Do not emit a place token merely because it appears in a title, topic,
  descriptive example, meta-instruction, reservation note or URL. In
  particular, quoted examples inside “不要因为…自动加入” and
  “不要把…从否定句里截出来” are instructions to skip those occurrences. Emit
  the later occurrence that directly states the actual REFERENCE or EXCLUDED
  intent. Do not emit a bare destination city from an introduction as an
  activity.
  Offsets are Unicode code-point, zero-based, half-open.
- `atomic_place_name` may be non-null only when the selected span, after outer
  whitespace is removed, is exactly that standalone place name. Copy it
  character-for-character from that span: do not translate it, add a city or
  category suffix, remove an existing city prefix, or substitute a common name.
- A whole sentence, description, route instruction, booking note, phone number
  or URL is never an atomic place. When the exact span clearly is a place entity,
  `atomic_place_name` MUST equal that span; do not set it to null. Use null only
  when the source itself does not provide a reliable place-name boundary. Never
  invent or normalize a name.
- Classify every place mention from its local sentence as exactly one of:
  - `PLANNED`: the author has committed to visit or stop there;
  - `OPTIONAL`: it is a backup, time-permitting choice, or conditionally may be
    skipped. “如果当天太累，X可以完全不去” is OPTIONAL;
  - `REFERENCE`: somebody mentioned or recommended it, or the text says it is
    not part of the current arrangement without explicitly rejecting it;
  - `EXCLUDED`: the author unconditionally decided not to go, cancel, remove or
    exclude;
  - `PASS_THROUGH`: the itinerary only passes through or transfers there.
  Phrases such as “听说/网友提到/另一篇攻略/不是本次安排” are `REFERENCE`,
  not `EXCLUDED`. A quoted example inside an instruction is not a substitute
  for the later, actual local intent sentence.
- Apply this local priority without borrowing intent from another occurrence:
  skip meta-instructions first; then unconditional cancellation is
  `EXCLUDED`; a conditional choice or conditional skip is `OPTIONAL`; a pure
  pass/transfer is `PASS_THROUGH`; a recommendation, hearsay item or “not this
  trip's arrangement” is `REFERENCE`; only an explicit committed visit is
  `PLANNED`.
- Judge every real occurrence independently even when names repeat or overlap.
  A planned compound such as `北京鼓楼` does not satisfy a later standalone
  `鼓楼` reference. Likewise, a quoted `X很有名` meta-example must be skipped
  without suppressing the later sentence that actually classifies `X` as a
  `REFERENCE`.
- Do not return day or sequence indexes. Application code derives the day from
  the nearest preceding Day/第N天 heading (Day 1 when absent), sorts accepted
  mentions by source span, and assigns sequence indexes.
- Do not return category or time hints. Provider resolution supplies verified
  categories later; application code copies only explicit local time markers
  such as “上午” or “08:30” from the source.

Do not claim that any POI, city, category, route, opening time or booking fact is
verified. Deterministic application code performs Provider resolution later.

Example rule: in “Day 1 上午去故宫博物院，预约说明见链接”, emit only the exact
“故宫博物院” span as the place mention and set `atomic_place_name` to the same
text. Do not emit “预约说明见链接” as a place mention.
