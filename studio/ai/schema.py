"""Turn Pydantic models into the JSON-schema subset every provider accepts.

Providers constrain *structure* (types, enums, required keys). Length / count limits are
not portable across providers, so they are stripped here and enforced client-side by
Pydantic validation in the router.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

_DROP = {"title", "default", "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems",
         "exclusiveMinimum", "exclusiveMaximum", "pattern", "examples", "format", "uniqueItems"}


def provider_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
            out = {}
            for k, v in node.items():
                if k == "properties":
                    out[k] = {name: walk(sub) for name, sub in v.items()}   # keep field names
                elif k not in _DROP:
                    out[k] = walk(v)
            if out.get("type") == "object" or "properties" in out:
                out["type"] = "object"
                out["additionalProperties"] = False
                out["required"] = list(out.get("properties", {}).keys())
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(raw)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> Any:
    """Parse model output as JSON. Tolerates code fences / leading prose, nothing else."""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    text = _FENCE.sub("", text).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model output")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])   # first complete object; ignore trailing text
    return obj


def schema_prompt(schema: dict[str, Any]) -> str:
    """Schema text appended to prompts for providers that do not strictly enforce it."""
    return ("\n\n---\nOUTPUT FORMAT: reply with ONE JSON object only (no markdown fences, no commentary) "
            "that validates against this JSON Schema. Use exactly these keys:\n"
            + json.dumps(schema, ensure_ascii=False))


def short_errors(err: Exception, limit: int = 12) -> str:
    if isinstance(err, ValidationError):
        lines = []
        for e in err.errors()[:limit]:
            loc = ".".join(str(p) for p in e.get("loc", ()))
            lines.append(f"- {loc}: {e.get('msg')}")
        return "\n".join(lines)
    return f"- {err}"
