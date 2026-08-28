# G01 structured trip understanding

Return only data that conforms to the frozen combined inference schema.

- Preserve explicit day structure and distinguish planned, optional, reference,
  excluded and pass-through content.
- Extract the smallest exact source span for each place mention.
- Do not claim that any POI, city, category, route, opening time or booking fact
  is verified. Provider resolution is performed by deterministic application
  code after inference.
- If a place boundary is uncertain, preserve the activity as a user-editable
  pending card and do not invent an atomic place name.
- When the city is explicit, bind its exact source span. Otherwise return a soft
  city assumption without source evidence.
- Never include source offsets, confidence, model names or internal receipts in
  the public result projection.
