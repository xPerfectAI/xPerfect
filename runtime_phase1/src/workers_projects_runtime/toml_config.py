"""Shared structured Codex configuration rendering and overlay semantics."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from pathlib import Path

def _toml_string(value: object) -> str:
    # JSON allows raw DEL; TOML basic strings require it to be escaped.
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_value(value: object, *, manifest_dir: Path | None = None, key: str = "") -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, str):
        rendered = str(manifest_dir) if key == "cwd" and value == "." and manifest_dir else value
        return _toml_string(rendered)
    if isinstance(value, dict):
        # TOML arrays of tables (for example native skill selections) arrive as
        # lists of dicts. Preserve them as equivalent inline tables.
        fields: list[str] = []
        for field, item in value.items():
            rendered = _toml_value(item)
            if rendered is None:
                return None
            fields.append(f"{_toml_table_name(str(field))} = {rendered}")
        return "{ " + ", ".join(fields) + " }"
    if isinstance(value, list):
        rendered_items: list[str] = []
        for item in value:
            item_rendered = _toml_value(item)
            if item_rendered is None:
                return None
            rendered_items.append(item_rendered)
        return "[" + ", ".join(rendered_items) + "]"
    return None


def _toml_table_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _toml_string(name)


def _render_toml_document(data: dict[str, object]) -> str:
    root_lines: list[str] = []
    table_blocks: list[str] = []
    for key, value in data.items():
        key_text = str(key)
        if isinstance(value, dict):
            table_blocks.extend(_render_toml_table([key_text], value))
            continue
        rendered = _toml_value(value)
        if rendered is not None:
            root_lines.append(f"{_toml_table_name(key_text)} = {rendered}")
    blocks: list[str] = []
    if root_lines:
        blocks.append("\n".join(root_lines))
    blocks.extend(table_blocks)
    return "\n\n".join(block for block in blocks if block.strip()).strip()


def _render_toml_table(path: list[str], table: dict[str, object]) -> list[str]:
    scalar_lines: list[str] = []
    nested_blocks: list[str] = []
    for key, value in table.items():
        key_text = str(key)
        if isinstance(value, dict):
            nested_blocks.extend(_render_toml_table([*path, key_text], value))
            continue
        rendered = _toml_value(value)
        if rendered is not None:
            scalar_lines.append(f"{_toml_table_name(key_text)} = {rendered}")
    blocks: list[str] = []
    if scalar_lines or not table:
        table_name = ".".join(_toml_table_name(part) for part in path)
        blocks.append("\n".join([f"[{table_name}]", *scalar_lines]))
    blocks.extend(nested_blocks)
    return blocks


def _overlay_toml_tables(base: dict, current: dict) -> dict:
    merged = dict(base)
    for key, value in current.items():
        previous = merged.get(key)
        merged[key] = (_overlay_toml_tables(previous, value)
                       if isinstance(previous, dict) and isinstance(value, dict) else value)
    return merged

