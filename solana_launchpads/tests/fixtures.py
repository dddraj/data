"""A Borsh *encoder* driven by the same IDL layouts the decoder uses.

Having one lets the tests build byte-exact account fixtures for every
launchpad without needing a live cluster, and it round-trips the layout
compiler for free: if the encoder and decoder disagree, one of them is wrong.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launchpad_decoder.anchor_idl import (  # noqa: E402
    ProgramSchema,
    StructLayout,
    to_snake,
)
from launchpad_decoder.base58 import b58decode  # noqa: E402

ZERO_PUBKEY = "11111111111111111111111111111111"

_INT_FORMATS = {
    "u8": (1, False),
    "i8": (1, True),
    "u16": (2, False),
    "i16": (2, True),
    "u32": (4, False),
    "i32": (4, True),
    "u64": (8, False),
    "i64": (8, True),
    "u128": (16, False),
    "i128": (16, True),
    "u256": (32, False),
    "i256": (32, True),
}


def _encode_node(node: Any, value: Any, types: Dict[str, dict]) -> bytes:
    if isinstance(node, str):
        name = node.lower()
        if name in _INT_FORMATS:
            size, signed = _INT_FORMATS[name]
            return int(value or 0).to_bytes(size, "little", signed=signed)
        if name == "bool":
            return bytes([1 if value else 0])
        if name == "f32":
            return struct.pack("<f", float(value or 0.0))
        if name == "f64":
            return struct.pack("<d", float(value or 0.0))
        if name in ("pubkey", "publickey"):
            return b58decode(value or ZERO_PUBKEY)
        if name == "string":
            raw = (value or "").encode()
            return len(raw).to_bytes(4, "little") + raw
        if name == "bytes":
            raw = value or b""
            return len(raw).to_bytes(4, "little") + raw
        raise ValueError(f"cannot encode primitive {node!r}")

    if "defined" in node:
        ref = node["defined"]
        type_name = ref if isinstance(ref, str) else ref["name"]
        return _encode_defined(type_name, value, types)
    if "option" in node:
        if value is None:
            return b"\x00"
        return b"\x01" + _encode_node(node["option"], value, types)
    if "vec" in node:
        items = value or []
        out = len(items).to_bytes(4, "little")
        return out + b"".join(_encode_node(node["vec"], item, types) for item in items)
    if "array" in node:
        inner, size = node["array"]
        items = list(value or [])
        items += [None] * (size - len(items))
        return b"".join(_encode_node(inner, item, types) for item in items[:size])
    raise ValueError(f"cannot encode node {node!r}")


def _encode_defined(type_name: str, value: Any, types: Dict[str, dict]) -> bytes:
    node = types.get(type_name)
    if node is None:
        raise ValueError(f"unknown type {type_name!r}")
    body = node.get("type", node)
    kind = body.get("kind")
    if kind == "struct":
        return _encode_struct(body.get("fields") or [], value or {}, types)
    if kind == "enum":
        variants = body.get("variants") or []
        wanted = value if isinstance(value, str) else (value or {}).get("variant")
        for index, variant in enumerate(variants):
            if variant.get("name") == wanted or (wanted is None and index == 0):
                out = bytes([index])
                if variant.get("fields"):
                    out += _encode_struct(variant["fields"], value or {}, types)
                return out
        raise ValueError(f"unknown variant {wanted!r} of {type_name!r}")
    raise ValueError(f"cannot encode type kind {kind!r}")


def _encode_struct(fields: List[dict], value: Dict[str, Any], types: Dict[str, dict]) -> bytes:
    out = b""
    for field in fields:
        key = to_snake(field["name"])
        out += _encode_node(field["type"], value.get(key), types)
    return out


def type_nodes(idl: dict) -> Dict[str, dict]:
    nodes: Dict[str, dict] = {}
    for section in ("types", "accounts", "events"):
        for node in idl.get(section) or []:
            if node.get("name") and "type" in node:
                nodes.setdefault(node["name"], node)
    for node in idl.get("types") or []:
        if node.get("name") and "type" in node:
            nodes[node["name"]] = node
    return nodes


def encode_account(idl: dict, schema: ProgramSchema, account_name: str, value: Dict[str, Any]) -> bytes:
    """Discriminator + borsh body for one account, from plain Python values."""
    types = type_nodes(idl)
    node = types[account_name]
    body = node.get("type", node)
    payload = _encode_struct(body.get("fields") or [], value, types)
    return schema.account(account_name).discriminator + payload


def layout_field_names(layout: StructLayout) -> Tuple[str, ...]:
    return tuple(name for name, _ in layout.fields)
