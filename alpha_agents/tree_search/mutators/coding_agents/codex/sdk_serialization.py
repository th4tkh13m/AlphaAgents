"""Shared SDK evidence serialization with an explicit legacy compatibility mode."""
from dataclasses import fields, is_dataclass
from types import SimpleNamespace


def to_jsonable(value, *, legacy_strings=False):
    """Retain structured tool evidence; preserve DGM's existing scalar format."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item, legacy_strings=legacy_strings) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item, legacy_strings=legacy_strings) for item in value]
    if legacy_strings:
        return str(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, SimpleNamespace):
        return to_jsonable(vars(value))
    if is_dataclass(value):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    raise TypeError(f"Cannot preserve SDK telemetry object {type(value).__name__}")
