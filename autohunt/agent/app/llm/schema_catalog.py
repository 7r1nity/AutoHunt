"""Lossless shared parameter definitions for the planner's capability document.

This is a model-facing projection, never the execution registry. References in
the returned document are relative to that document, not the enclosing prompt.
All tools and constraints survive expansion; nothing is selected by user wording.
Native provider tool schemas continue to come from the unmodified registry.
"""
from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Any


# Only visit JSON Schema positions. A properties map, enum value or example is
# data, and replacing it with a $ref would change the schema's meaning.
_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SINGLES = frozenset({"additionalProperties", "additionalItems", "contains", "not", "if", "then", "else",
                      "propertyNames", "unevaluatedProperties", "unevaluatedItems", "contentSchema"})
_SCOPED = frozenset({"$id", "$ref", "$dynamicRef", "$recursiveRef", "$anchor", "$dynamicAnchor",
                     "$recursiveAnchor", "$defs", "definitions"})
_MIN_DEFINITION_CHARS = 110


def _children(schema: dict):
    for key, value in schema.items():
        if key in _MAPS and isinstance(value, dict):
            yield from (entry for entry in value.values() if isinstance(entry, dict))
        elif key in _LISTS and isinstance(value, list):
            yield from (entry for entry in value if isinstance(entry, dict))
        elif key == "items":
            if isinstance(value, dict):
                yield value
            elif isinstance(value, list):
                yield from (entry for entry in value if isinstance(entry, dict))
        elif key in _SINGLES and isinstance(value, dict):
            yield value


def _key(schema: dict) -> str:
    return json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _has_reference_scope(schema: dict) -> bool:
    return bool(_SCOPED.intersection(schema)) or any(_has_reference_scope(child) for child in _children(schema))


def compact_tool_catalog(schemas: list[dict[str, Any]]) -> dict:
    """Return a self-contained catalog with repeated schema nodes stored once.

    Parameter schemas with existing reference scopes remain opaque: moving a
    node containing $id/$ref could retarget its references. Other parameter
    schemas can still be shared. The input is never mutated.
    """
    counts: Counter[str] = Counter()

    def count(schema: dict) -> None:
        for child in _children(schema):
            count(child)
        encoded = _key(schema)
        if len(encoded) >= _MIN_DEFINITION_CHARS:
            counts[encoded] += 1

    for tool in schemas:
        parameters = tool.get("function", {}).get("parameters")
        if isinstance(parameters, dict) and not _has_reference_scope(parameters):
            count(parameters)

    # Local names are deterministic catalog indexes, not persistent identities
    # or integrity digests. Short names also keep repeated references small.
    names = {key: "p" + str(index) for index, key in enumerate(sorted(
        key for key, count in counts.items() if count > 1))}
    definitions: dict[str, dict] = {}

    def rewrite(schema: dict, *, inline=False) -> dict:
        encoded = _key(schema)
        name = names.get(encoded)
        if name and not inline:
            if name not in definitions:
                definitions[name] = rewrite(schema, inline=True)
            return {"$ref": "#/$defs/" + name}
        result = copy.deepcopy(schema)
        for key, value in schema.items():
            if key in _MAPS and isinstance(value, dict):
                result[key] = {name: rewrite(entry) if isinstance(entry, dict) else copy.deepcopy(entry)
                               for name, entry in value.items()}
            elif (key in _LISTS or key == "items") and isinstance(value, list):
                result[key] = [rewrite(entry) if isinstance(entry, dict) else copy.deepcopy(entry) for entry in value]
            elif (key in _SINGLES or key == "items") and isinstance(value, dict):
                result[key] = rewrite(value)
        return result

    tools = copy.deepcopy(schemas)
    self_contained = []
    for tool in tools:
        function = tool.get("function", {})
        if isinstance(function.get("parameters"), dict):
            if _has_reference_scope(function["parameters"]):
                self_contained.append(function["name"])
            else:
                function["parameters"] = rewrite(function["parameters"])
    catalog = {"$defs": dict(sorted(definitions.items())), "tools": tools}
    if self_contained:
        catalog["self_contained_parameters"] = self_contained
    return catalog
