#!/usr/bin/env python3
"""Regenerate ``config/config.schema.json`` from the live pydantic Settings model."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from pydantic import BaseModel

from robotsix_chat.config import Settings
from robotsix_chat.config import models as _config_models

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "config" / "config.schema.json"


def _parse_attributes_section(cls: type[BaseModel]) -> dict[str, str]:
    """Extract field descriptions from a class docstring's ``Attributes:`` section.

    Returns a mapping of field-name → description-text (with ``Default …``
    suffix stripped for brevity in JSON Schema).
    """
    doc = inspect.getdoc(cls)
    if not doc:
        return {}

    lines = doc.split("\n")
    # Locate the ``Attributes:`` line.
    attrs_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "Attributes:":
            attrs_idx = i
            break
    if attrs_idx is None:
        return {}

    # Walk entries from the next line onward.
    result: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    for line in lines[attrs_idx + 1 :]:
        stripped = line.strip()

        # Blank line or dedent below attribute-indent level terminates the block.
        if not stripped and not current_name:
            continue
        if not stripped and current_name:
            result[current_name] = _clean_desc(" ".join(current_lines))
            current_name = None
            current_lines = []
            continue

        # Heuristic: a new attribute entry starts at the indentation level of
        # the ``Attributes:`` line itself.  The ``Attributes:`` line has some
        # base indent; attribute entries are indented by exactly 4 more spaces
        # in the source (reStructuredText convention).
        # We detect "new entry" by looking for a line that begins with a word
        # character and contains a ``:`` before the end of the name.
        is_new_entry = bool(re.match(r"^[a-z_][a-z_0-9]*\s*:", stripped))
        if is_new_entry:
            if current_name:
                result[current_name] = _clean_desc(" ".join(current_lines))
            name_part, _, desc_part = stripped.partition(":")
            current_name = name_part.strip()
            current_lines = [desc_part.strip()] if desc_part.strip() else []
        elif current_name:
            current_lines.append(stripped)

    if current_name:
        result[current_name] = _clean_desc(" ".join(current_lines))

    return result


def _clean_desc(text: str) -> str:
    """Strip trailing ``Default …`` clause and collapse whitespace."""
    # Remove trailing "Default ``value``." or "Default ``value``, …" patterns.
    text = re.sub(r"\s*Default\s+``[^`]*``\s*[.,]?\s*$", "", text).rstrip(".")
    # Collapse multiple spaces.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _inject_descriptions(schema: dict) -> dict:
    """Walk a generated JSON Schema and add ``description`` fields.

    Wherever the source model class's docstring provides one via its
    ``Attributes:`` section.
    """
    # Build a mapping of $def name → class for every model in the config
    # module that is a BaseModel subclass.
    model_map: dict[str, type[BaseModel]] = {}
    for name, obj in vars(_config_models).items():
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseModel)
            and obj is not BaseModel
        ):
            model_map[name] = obj
    # Also add the top-level Settings class.
    model_map["Settings"] = Settings

    # Helper: apply descriptions to a properties dict from a parsed mapping.
    def _apply(properties: dict, desc_map: dict[str, str]) -> None:
        for prop_name, prop_schema in properties.items():
            if "description" not in prop_schema and prop_name in desc_map:
                prop_schema["description"] = desc_map[prop_name]

    # Top-level Settings properties.
    top_descs = _parse_attributes_section(Settings)
    _apply(schema.get("properties", {}), top_descs)

    # Each $def.
    for def_name, def_schema in schema.get("$defs", {}).items():
        cls = model_map.get(def_name)
        if cls is None:
            continue
        desc_map = _parse_attributes_section(cls)
        _apply(def_schema.get("properties", {}), desc_map)

    return schema


def main() -> None:
    """Regenerate the committed schema file from the live Settings model."""
    schema = Settings.model_json_schema()
    schema = _inject_descriptions(schema)
    _SCHEMA_PATH.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {_SCHEMA_PATH}")


if __name__ == "__main__":
    main()
