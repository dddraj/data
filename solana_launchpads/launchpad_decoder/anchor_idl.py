"""Compile an Anchor IDL into account decoders.

This is the piece that makes the whole decoder *program level* rather than
*launchpad level*: nothing here knows what a bonding curve is.  Give it any
Anchor IDL -- bundled in this repo, downloaded, or read straight off the chain
from the program's IDL PDA -- and it produces a `ProgramSchema` that can turn
raw account bytes into Python dicts, keyed by the 8-byte account discriminator
that the program itself writes.

Both IDL dialects are handled:

* the pre-0.30 ("legacy") dialect, where ``accounts`` carry an inline
  ``type``, fields are camelCase, pubkeys are spelled ``publicKey`` and
  user types are referenced as ``{"defined": "Name"}``;
* the 0.30+ dialect, where ``accounts`` carry an explicit ``discriminator``,
  layouts live in ``types``, fields are snake_case, pubkeys are spelled
  ``pubkey`` and user types are ``{"defined": {"name": "Name"}}``.

Field names are normalised to snake_case on output so downstream adapters see
one spelling regardless of which dialect the program shipped.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .borsh import BorshError, BorshReader

_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")

DISCRIMINATOR_LEN = 8


def to_snake(name: str) -> str:
    """``baseTokenVaultBalance`` -> ``base_token_vault_balance`` (idempotent)."""
    step = _CAMEL_BOUNDARY_1.sub(r"\1_\2", name)
    step = _CAMEL_BOUNDARY_2.sub(r"\1_\2", step)
    return step.replace("__", "_").lower()


def account_discriminator(name: str) -> bytes:
    """Anchor's default account discriminator: sha256("account:<Name>")[:8]."""
    return hashlib.sha256(f"account:{name}".encode()).digest()[:DISCRIMINATOR_LEN]


def event_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:DISCRIMINATOR_LEN]


class IdlError(ValueError):
    pass


# --------------------------------------------------------------------------
# type compilation
# --------------------------------------------------------------------------

Decoder = Callable[[BorshReader], Any]

_PRIMITIVES: Dict[str, str] = {
    "bool": "boolean",
    "u8": "u8",
    "i8": "i8",
    "u16": "u16",
    "i16": "i16",
    "u32": "u32",
    "i32": "i32",
    "u64": "u64",
    "i64": "i64",
    "u128": "u128",
    "i128": "i128",
    "u256": "u256",
    "i256": "i256",
    "f32": "f32",
    "f64": "f64",
    "string": "string",
    "publickey": "pubkey",
    "pubkey": "pubkey",
    "bytes": "byte_vec",
}


@dataclass
class StructLayout:
    """A named struct plus the ordered decoders for its fields."""

    name: str
    fields: List[Tuple[str, Decoder]]
    #: byte offset of each field *within the struct body*, for every field
    #: reachable without crossing a variable-length one.  Add
    #: `DISCRIMINATOR_LEN` to get the offset within the account, which is what
    #: an RPC `memcmp` filter wants.
    offsets: Dict[str, int] = field(default_factory=dict)
    #: byte width of each field, or None where it is variable-length
    sizes: Dict[str, Optional[int]] = field(default_factory=dict)

    def memcmp_offset(self, field_name: str) -> Optional[int]:
        """Offset of `field_name` within the raw account, or None if unknowable."""
        body_offset = self.offsets.get(field_name)
        return None if body_offset is None else DISCRIMINATOR_LEN + body_offset

    def pubkey_fields(self) -> List[str]:
        """Fields that are 32 bytes wide and sit at a known offset."""
        return [
            name
            for name, _decoder in self.fields
            if self.sizes.get(name) == 32 and name in self.offsets
        ]

    def decode(self, reader: BorshReader) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for field_name, decoder in self.fields:
            out[field_name] = decoder(reader)
        return out

    def decode_prefix(self, reader: BorshReader) -> Tuple[Dict[str, Any], List[str]]:
        """Decode as many leading fields as the buffer holds.

        Anchor programs routinely append fields to an existing account struct
        and only realloc accounts lazily, so a live cluster contains accounts
        written by several generations of the same program.  Decoding the
        common prefix and reporting what was missing beats refusing to decode.
        """
        out: Dict[str, Any] = {}
        missing: List[str] = []
        exhausted = False
        for field_name, decoder in self.fields:
            if exhausted:
                missing.append(field_name)
                continue
            mark = reader.offset
            try:
                out[field_name] = decoder(reader)
            except BorshError:
                reader.offset = mark
                exhausted = True
                missing.append(field_name)
        return out, missing

    def decode_bytes(self, data: bytes, offset: int = 0) -> Dict[str, Any]:
        return self.decode(BorshReader(data, offset))

    def decode_bytes_prefix(
        self, data: bytes, offset: int = 0
    ) -> Tuple[Dict[str, Any], List[str]]:
        return self.decode_prefix(BorshReader(data, offset))


#: byte width of every fixed-size primitive
_PRIMITIVE_SIZES: Dict[str, int] = {
    "bool": 1,
    "u8": 1,
    "i8": 1,
    "u16": 2,
    "i16": 2,
    "u32": 4,
    "i32": 4,
    "f32": 4,
    "u64": 8,
    "i64": 8,
    "f64": 8,
    "u128": 16,
    "i128": 16,
    "u256": 32,
    "i256": 32,
    "publickey": 32,
    "pubkey": 32,
}


def fixed_size(node: Any, types: Dict[str, dict], _seen: Optional[set] = None) -> Optional[int]:
    """Byte width of an IDL type, or None when it is variable-length.

    Borsh lays fields out back to back with no padding, so summing these gives
    the exact offset of a field -- which is what lets the decoder turn "find
    the curve account for this mint" into a server-side `memcmp` instead of a
    full program scan.
    """
    _seen = _seen or set()
    if isinstance(node, str):
        return _PRIMITIVE_SIZES.get(node.lower())
    if not isinstance(node, dict):
        return None
    if "array" in node:
        inner, count = node["array"]
        if not isinstance(count, int):
            return None
        inner_size = fixed_size(inner, types, _seen)
        return None if inner_size is None else inner_size * count
    if "defined" in node:
        ref = node["defined"]
        name = ref if isinstance(ref, str) else ref.get("name")
        if not name or name in _seen:
            return None
        body = (types.get(name) or {}).get("type")
        if not body:
            return None
        if body.get("kind") == "struct":
            return _struct_size(body.get("fields") or [], types, _seen | {name})
        if body.get("kind") == "enum":
            variants = body.get("variants") or []
            # A C-like enum is one byte; a data-carrying one is not fixed width.
            return 1 if variants and not any(v.get("fields") for v in variants) else None
        return None
    # vec / string / bytes / option / coption are all variable-length
    return None


def _struct_size(fields: List[Any], types: Dict[str, dict], seen: set) -> Optional[int]:
    total = 0
    for f in fields:
        node = f["type"] if isinstance(f, dict) else f
        size = fixed_size(node, types, seen)
        if size is None:
            return None
        total += size
    return total


def field_offsets(
    fields: List[Any], types: Dict[str, dict]
) -> Tuple[Dict[str, int], Dict[str, Optional[int]]]:
    """Offsets and widths of a struct's fields.

    Offsets stop at the first variable-length field, since everything after it
    has no fixed offset; widths are reported for every field either way.
    """
    offsets: Dict[str, int] = {}
    sizes: Dict[str, Optional[int]] = {}
    cursor: Optional[int] = 0
    for idx, f in enumerate(fields):
        if isinstance(f, dict):
            name = to_snake(f.get("name") or f"field_{idx}")
            node = f["type"]
        else:
            name, node = f"field_{idx}", f
        size = fixed_size(node, types)
        sizes[name] = size
        if cursor is not None:
            offsets[name] = cursor
            cursor = None if size is None else cursor + size
    return offsets, sizes


class _TypeCompiler:
    """Compiles IDL type nodes into closures, resolving `defined` lazily."""

    def __init__(self, type_nodes: Dict[str, dict]) -> None:
        self._nodes = type_nodes
        self._cache: Dict[str, Decoder] = {}
        self._in_progress: set = set()

    # -- public ---------------------------------------------------------
    def struct_layout(self, name: str) -> StructLayout:
        node = self._nodes.get(name)
        if node is None:
            raise IdlError(f"IDL has no type definition named {name!r}")
        type_node = node.get("type", node)
        if type_node.get("kind") != "struct":
            raise IdlError(f"type {name!r} is not a struct")
        fields = type_node.get("fields") or []
        offsets, sizes = field_offsets(fields, self._nodes)
        return StructLayout(name, self._compile_struct_fields(fields), offsets, sizes)

    def compile(self, node: Any) -> Decoder:
        if isinstance(node, str):
            return self._primitive(node)
        if not isinstance(node, dict):
            raise IdlError(f"unsupported IDL type node: {node!r}")

        if "defined" in node:
            return self._defined(node["defined"])
        if "option" in node:
            return self._option(self.compile(node["option"]))
        if "coption" in node:  # rare, emitted by a few non-Anchor IDLs
            return self._coption(self.compile(node["coption"]))
        if "vec" in node:
            return self._vec(self.compile(node["vec"]))
        if "array" in node:
            inner, size = node["array"]
            return self._array(self.compile(inner), self._array_size(size))
        if "generic" in node:
            raise IdlError("generic IDL types are not supported")
        raise IdlError(f"unsupported IDL type node: {node!r}")

    # -- internals ------------------------------------------------------
    @staticmethod
    def _array_size(size: Any) -> int:
        if isinstance(size, int):
            return size
        if isinstance(size, dict) and "generic" in size:
            raise IdlError("generic array length is not supported")
        raise IdlError(f"unsupported array length {size!r}")

    @staticmethod
    def _primitive(name: str) -> Decoder:
        method = _PRIMITIVES.get(name.lower())
        if method is None:
            raise IdlError(f"unknown primitive type {name!r}")
        return lambda reader, _m=method: getattr(reader, _m)()

    @staticmethod
    def _option(inner: Decoder) -> Decoder:
        def decode(reader: BorshReader) -> Any:
            tag = reader.u8()
            if tag == 0:
                return None
            if tag != 1:
                raise BorshError(f"invalid Option tag {tag}")
            return inner(reader)

        return decode

    @staticmethod
    def _coption(inner: Decoder) -> Decoder:
        def decode(reader: BorshReader) -> Any:
            tag = reader.u32()
            return inner(reader) if tag else None

        return decode

    @staticmethod
    def _vec(inner: Decoder) -> Decoder:
        def decode(reader: BorshReader) -> List[Any]:
            return [inner(reader) for _ in range(reader.u32())]

        return decode

    @staticmethod
    def _array(inner: Decoder, size: int) -> Decoder:
        def decode(reader: BorshReader) -> Any:
            return [inner(reader) for _ in range(size)]

        return decode

    def _defined(self, ref: Any) -> Decoder:
        name = ref if isinstance(ref, str) else ref.get("name")
        if not name:
            raise IdlError(f"malformed defined-type reference {ref!r}")
        if name in self._cache:
            return self._cache[name]
        if name in self._in_progress:
            # Self-referential type: resolve through the cache at call time.
            return lambda reader, _n=name: self._cache[_n](reader)

        node = self._nodes.get(name)
        if node is None:
            raise IdlError(f"IDL references undefined type {name!r}")

        self._in_progress.add(name)
        try:
            decoder = self._compile_type_node(name, node.get("type", node))
        finally:
            self._in_progress.discard(name)
        self._cache[name] = decoder
        return decoder

    def _compile_type_node(self, name: str, type_node: dict) -> Decoder:
        kind = type_node.get("kind")
        if kind == "struct":
            layout = StructLayout(name, self._compile_struct_fields(type_node.get("fields") or []))
            return layout.decode
        if kind == "enum":
            return self._compile_enum(name, type_node.get("variants") or [])
        if kind == "type":  # type alias
            return self.compile(type_node["alias"])
        raise IdlError(f"unsupported type kind {kind!r} for {name!r}")

    def _compile_struct_fields(self, fields: Iterable[Any]) -> List[Tuple[str, Decoder]]:
        compiled: List[Tuple[str, Decoder]] = []
        for idx, f in enumerate(fields):
            if isinstance(f, dict):
                raw_name = f.get("name") or f"field_{idx}"
                compiled.append((to_snake(raw_name), self.compile(f["type"])))
            else:  # tuple struct: positional fields
                compiled.append((f"field_{idx}", self.compile(f)))
        return compiled

    def _compile_enum(self, name: str, variants: List[dict]) -> Decoder:
        prepared: List[Tuple[str, Optional[List[Tuple[str, Decoder]]]]] = []
        for variant in variants:
            vname = variant.get("name", "")
            vfields = variant.get("fields")
            if not vfields:
                prepared.append((vname, None))
            else:
                prepared.append((vname, self._compile_struct_fields(vfields)))

        def decode(reader: BorshReader) -> Any:
            index = reader.u8()
            if index >= len(prepared):
                raise BorshError(f"enum {name}: variant index {index} out of range")
            vname, vfields = prepared[index]
            if vfields is None:
                return vname
            payload = {fname: dec(reader) for fname, dec in vfields}
            return {"variant": vname, "index": index, **payload}

        return decode


# --------------------------------------------------------------------------
# program schema
# --------------------------------------------------------------------------


@dataclass
class AccountSchema:
    name: str
    discriminator: bytes
    layout: StructLayout

    def decode(self, data: bytes, *, strict: bool = False) -> Dict[str, Any]:
        if not data.startswith(self.discriminator):
            raise IdlError(
                f"account data does not start with the {self.name} discriminator"
            )
        if strict:
            return self.layout.decode_bytes(data, DISCRIMINATOR_LEN)
        decoded, missing = self.layout.decode_bytes_prefix(data, DISCRIMINATOR_LEN)
        if missing:
            decoded["_truncated_fields"] = missing
        return decoded


@dataclass
class ProgramSchema:
    """Everything needed to decode one program's accounts and events."""

    program_id: Optional[str]
    name: str
    version: str
    accounts: Dict[str, AccountSchema] = field(default_factory=dict)
    events: Dict[str, AccountSchema] = field(default_factory=dict)
    source: str = "unknown"
    #: opaque marker of the IDL revision this schema came from
    fingerprint: str = ""
    _by_discriminator: Dict[bytes, AccountSchema] = field(default_factory=dict, repr=False)

    def account(self, name: str) -> AccountSchema:
        try:
            return self.accounts[name]
        except KeyError as exc:
            raise IdlError(
                f"program {self.name!r} has no account named {name!r}; "
                f"known accounts: {sorted(self.accounts)}"
            ) from exc

    def identify(self, data: bytes) -> Optional[AccountSchema]:
        """Classify raw account bytes by their leading discriminator."""
        if len(data) < DISCRIMINATOR_LEN:
            return None
        return self._by_discriminator.get(bytes(data[:DISCRIMINATOR_LEN]))

    def decode_account(
        self, data: bytes, *, strict: bool = False
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        schema = self.identify(data)
        if schema is None:
            return None
        return schema.name, schema.decode(data, strict=strict)

    def decode_event(
        self, data: bytes, *, strict: bool = False
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Decode a self-CPI / `emit!` event payload (8-byte discriminator + borsh)."""
        if len(data) < DISCRIMINATOR_LEN:
            return None
        head = bytes(data[:DISCRIMINATOR_LEN])
        for schema in self.events.values():
            if schema.discriminator == head:
                return schema.name, schema.decode(data, strict=strict)
        return None


def _collect_type_nodes(idl: dict) -> Dict[str, dict]:
    nodes: Dict[str, dict] = {}
    for section in ("types", "accounts", "events"):
        for node in idl.get(section) or []:
            name = node.get("name")
            if not name:
                continue
            # Only register nodes that actually carry a layout. In the 0.30
            # dialect `accounts` entries are discriminator-only stubs and the
            # layout lives in `types`, which is registered first-wins here.
            if "type" in node and name not in nodes:
                nodes[name] = node
    for node in idl.get("types") or []:
        name = node.get("name")
        if name and "type" in node:
            nodes[name] = node  # `types` always wins
    return nodes


def _fingerprint(idl: dict) -> str:
    import json

    blob = json.dumps(idl, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def compile_idl(idl: dict, source: str = "unknown") -> ProgramSchema:
    """Turn a parsed Anchor IDL document into a `ProgramSchema`."""
    metadata = idl.get("metadata") or {}
    schema = ProgramSchema(
        program_id=idl.get("address") or metadata.get("address"),
        name=metadata.get("name") or idl.get("name") or "unknown",
        version=metadata.get("version") or idl.get("version") or "0.0.0",
        source=source,
        fingerprint=_fingerprint(idl),
    )

    nodes = _collect_type_nodes(idl)
    compiler = _TypeCompiler(nodes)

    for entry in idl.get("accounts") or []:
        name = entry.get("name")
        if not name:
            continue
        disc = entry.get("discriminator")
        discriminator = bytes(disc) if disc else account_discriminator(name)
        try:
            layout = compiler.struct_layout(name)
        except IdlError:
            # Discriminator-only stub with no matching layout -- skip it rather
            # than failing the whole program.
            continue
        account = AccountSchema(name=name, discriminator=discriminator, layout=layout)
        schema.accounts[name] = account
        schema._by_discriminator[discriminator] = account

    for entry in idl.get("events") or []:
        name = entry.get("name")
        if not name:
            continue
        disc = entry.get("discriminator")
        discriminator = bytes(disc) if disc else event_discriminator(name)
        try:
            layout = compiler.struct_layout(name)
        except IdlError:
            continue
        schema.events[name] = AccountSchema(name, discriminator, layout)

    return schema
