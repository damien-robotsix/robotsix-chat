"""Fill in per-property ``description`` on the exported settings JSON schema.

Pydantic only emits a property ``description`` when a field carries an
explicit ``Field(description=...)``.  Most settings models instead document
their fields in a Google-style ``Attributes:`` section of the class
docstring, which lands in the schema as the model's own ``description`` but
never reaches the individual properties the settings UI renders tooltips
for.

:func:`apply_property_descriptions` post-processes the generated schema so
that *every* property gets a non-empty ``description``:

1. Parse the owning object's ``Attributes:`` docstring section and copy each
   ``field: text`` entry onto the matching property.
2. For a property that references another model (``$ref``), fall back to that
   model's summary (the docstring paragraph before ``Attributes:``).
3. As a last resort, fall back to the property ``title`` so no property is
   ever left without a description.
"""

from __future__ import annotations

import re
from typing import Any

_FIELD_RE = re.compile(r"^(\s*)(\w+):\s?(.*)$")


def _parse_attributes(description: str) -> dict[str, str]:
    """Return ``{field_name: text}`` parsed from a Google ``Attributes:`` block.

    Field entries sit at a single indentation level; more-indented lines are
    continuations of the current field.  Returns an empty map when the
    docstring has no ``Attributes:`` section.
    """
    if not description:
        return {}
    lines = description.split("\n")
    start: int | None = None
    attr_indent = 0
    for idx, line in enumerate(lines):
        if line.strip() == "Attributes:":
            start = idx + 1
            attr_indent = len(line) - len(line.lstrip())
            break
    if start is None:
        return {}

    result: dict[str, str] = {}
    base_indent: int | None = None
    current: str | None = None
    for line in lines[start:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= attr_indent:
            break  # dedented out of the Attributes block
        match = _FIELD_RE.match(line)
        if match is not None and base_indent is None:
            base_indent = indent
        if match is not None and indent == base_indent:
            current = match.group(2)
            result[current] = match.group(3).strip()
        elif current is not None:
            result[current] = (result[current] + " " + line.strip()).strip()
    return result


def _summary(description: str) -> str:
    """Return the first docstring paragraph (before ``Attributes:``), collapsed."""
    if not description:
        return ""
    head = description.split("\nAttributes:", 1)[0]
    paragraph = head.strip().split("\n\n", 1)[0]
    return " ".join(part.strip() for part in paragraph.splitlines()).strip()


def _referenced_def(prop: dict[str, Any]) -> str | None:
    """Return the ``$defs`` name a property references, if any."""
    ref = prop.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in prop.get(key, []):
            if isinstance(sub, dict) and isinstance(sub.get("$ref"), str):
                return sub["$ref"].rsplit("/", 1)[-1]
    return None


def apply_property_descriptions(schema: dict[str, Any]) -> dict[str, Any]:
    """Ensure every property in *schema* carries a non-empty ``description``.

    Mutates *schema* in place (and also returns it) — the top-level object
    plus every entry under ``$defs`` are processed.
    """
    defs: dict[str, Any] = schema.get("$defs", {})

    def process(obj: dict[str, Any]) -> None:
        attrs = _parse_attributes(obj.get("description", ""))
        for name, prop in obj.get("properties", {}).items():
            if not isinstance(prop, dict) or prop.get("description"):
                continue
            text = attrs.get(name)
            if not text:
                ref = _referenced_def(prop)
                target = defs.get(ref) if ref else None
                if isinstance(target, dict):
                    text = _summary(target.get("description", ""))
            if not text:
                text = prop.get("title") or name
            prop["description"] = text

    process(schema)
    for definition in defs.values():
        if isinstance(definition, dict):
            process(definition)
    return schema
