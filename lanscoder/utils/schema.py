from __future__ import annotations

from typing import Any, Literal

JsonSchemaType = Literal["string", "integer", "boolean", "number", "object", "array"]


def property_schema(schema_type: JsonSchemaType, **extra: Any) -> dict[str, Any]:

    schema: dict[str, Any] = {"type": schema_type}
    schema.update(extra)
    return schema


def object_schema(properties: dict[str, dict[str, Any]], required: list[str] | None = None) -> dict[str, Any]:

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema
