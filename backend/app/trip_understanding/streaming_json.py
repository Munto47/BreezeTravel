"""Incrementally decode a JSON object's arrays without accepting partial values."""
from __future__ import annotations

import codecs
import json


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("non-finite JSON value")


class StreamingObject:
    """Emit (field, index-or-None, value) once per complete field/array item.

    JSONDecoder handles strings, escapes and nesting. Structural delimiters
    are consumed explicitly, including across chunks. finish validates the
    complete document, so a transport EOF is never a completion signal.
    """

    def __init__(self, *, max_characters=1_000_000):
        self.decoder = json.JSONDecoder(object_pairs_hook=unique_object, parse_constant=invalid_constant)
        self.utf8 = codecs.getincrementaldecoder("utf-8")("strict")
        self.text = ""
        self.position = 0
        self.state = "start"
        self.field = None
        self.fields = set()
        self.index = 0
        self.max_characters = max_characters
        self.completed_events = []

    def feed(self, chunk: str | bytes):
        self.text += self.utf8.decode(chunk) if isinstance(chunk, bytes) else chunk
        if len(self.text) > self.max_characters:
            raise ValueError("structured output exceeds size limit")
        events = []
        while True:
            while self.position < len(self.text) and self.text[self.position].isspace():
                self.position += 1
            if self.position == len(self.text):
                return events
            char = self.text[self.position]
            if self.state == "done":
                raise ValueError("trailing JSON data")
            if self.state in {"start", "colon", "field_separator", "array_separator"}:
                expected = {"start": "{", "colon": ":", "field_separator": ",}", "array_separator": ",]"}[self.state]
                if char not in expected:
                    raise ValueError("invalid JSON delimiter")
                self.position += 1
                if self.state == "start":
                    self.state = "first_key"
                elif self.state == "colon":
                    self.state = "value"
                elif self.state == "field_separator":
                    self.state = "key" if char == "," else "done"
                else:
                    self.state = "array_item" if char == "," else "field_separator"
                continue
            if self.state == "first_key" and char == "}":
                self.position += 1
                self.state = "done"
                continue
            if self.state == "value" and char == "[":
                self.position += 1
                self.index = 0
                self.state = "first_array_item"
                continue
            if self.state == "first_array_item" and char == "]":
                self.position += 1
                self.state = "field_separator"
                events.append((self.field, None, []))
                self.completed_events.append(events[-1])
                continue
            try:
                value, end = self.decoder.raw_decode(self.text, self.position)
            except json.JSONDecodeError:
                return events  # Incomplete or invalid; finish distinguishes them.
            except RecursionError as exc:
                raise ValueError("structured output nesting exceeds limit") from exc
            # Numbers may continue in the next chunk (1 -> 12 or 1e2).
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if end == len(self.text):
                    return events
                if self.text[end] not in " \t\r\n,]}":
                    return events
            self.position = end
            if self.state in {"key", "first_key"}:
                if not isinstance(value, str) or value in self.fields:
                    raise ValueError("invalid or duplicate JSON field")
                self.field = value
                self.fields.add(value)
                self.state = "colon"
            elif self.state in {"array_item", "first_array_item"}:
                events.append((self.field, self.index, value))
                self.completed_events.append(events[-1])
                self.index += 1
                self.state = "array_separator"
            else:
                events.append((self.field, None, value))
                self.completed_events.append(events[-1])
                self.state = "field_separator"

    def finish(self):
        self.feed(self.utf8.decode(b"", final=True))
        value = self.decoder.decode(self.text)
        if self.state != "done" or not isinstance(value, dict):
            raise ValueError("incomplete structured output")
        return value
