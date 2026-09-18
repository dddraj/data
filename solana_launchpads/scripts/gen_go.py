#!/usr/bin/env python3
"""Generate the Go account layouts from the bundled IDLs.

The Go package is the production decoder; this keeps its structs honest by
deriving them from the same IDL snapshots the Python side uses, rather than
letting someone hand-transcribe an offset. Re-run it after refreshing an IDL:

    python scripts/gen_go.py && (cd go && gofmt -w . && go test ./...)

Only the fixed-width prefix of each account is generated. Everything up to the
first variable-length field has a stable offset; nothing after it does, and a
launch's numbers all live in the prefix.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder.anchor_idl import (  # noqa: E402
    account_discriminator,
    field_offsets,
    fixed_size,
    to_snake,
)
from launchpad_decoder.registry import LAUNCHPADS  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"
OUT = ROOT / "go" / "launchpad" / "layouts_gen.go"

SCALARS = {
    "bool": ("bool", 1, "data[o] == 1"),
    "u8": ("uint8", 1, "data[o]"),
    "i8": ("int8", 1, "int8(data[o])"),
    "u16": ("uint16", 2, "binary.LittleEndian.Uint16(data[o:])"),
    "i16": ("int16", 2, "int16(binary.LittleEndian.Uint16(data[o:]))"),
    "u32": ("uint32", 4, "binary.LittleEndian.Uint32(data[o:])"),
    "i32": ("int32", 4, "int32(binary.LittleEndian.Uint32(data[o:]))"),
    "f32": ("float32", 4, "math.Float32frombits(binary.LittleEndian.Uint32(data[o:]))"),
    "u64": ("uint64", 8, "binary.LittleEndian.Uint64(data[o:])"),
    "i64": ("int64", 8, "int64(binary.LittleEndian.Uint64(data[o:]))"),
    "f64": ("float64", 8, "math.Float64frombits(binary.LittleEndian.Uint64(data[o:]))"),
    "u128": ("U128", 16, "u128At(data, o)"),
    "i128": ("U128", 16, "u128At(data, o)"),
    "pubkey": ("Pubkey", 32, "pubkeyAt(data, o)"),
    "publickey": ("Pubkey", 32, "pubkeyAt(data, o)"),
}


def go_name(raw: str) -> str:
    """snake_case / camelCase -> exported Go identifier."""
    parts = [p for p in to_snake(raw).split("_") if p]
    out = "".join(p[:1].upper() + p[1:] for p in parts)
    for acronym, replacement in (("Lp", "LP"), ("Amm", "AMM"), ("Nft", "NFT")):
        if out.startswith(acronym) and len(out) > len(acronym):
            out = replacement + out[len(acronym) :]
    return out or "Field"


class Generator:
    def __init__(self) -> None:
        self.structs: Dict[str, str] = {}
        self.order: List[str] = []
        self.accounts: List[Tuple[str, str, bytes]] = []

    def resolve(self, node, nodes, prefix) -> Optional[Tuple[str, int, str]]:
        """(go type, byte width, expression) for a fixed-width IDL type."""
        if isinstance(node, str):
            return SCALARS.get(node.lower())
        if not isinstance(node, dict):
            return None
        if "array" in node:
            inner, count = node["array"]
            if not isinstance(count, int):
                return None
            got = self.resolve(inner, nodes, prefix)
            if got is None:
                return None
            gotype, width, inner_expr = got
            elem_kind = "struct" if inner_expr.startswith("decodeNested:") else "scalar"
            return (f"[{count}]{gotype}", width * count, f"decodeArray:{elem_kind}")
        if "defined" in node:
            ref = node["defined"]
            name = ref if isinstance(ref, str) else ref.get("name")
            body = (nodes.get(name) or {}).get("type") or {}
            if body.get("kind") == "enum":
                variants = body.get("variants") or []
                if variants and not any(v.get("fields") for v in variants):
                    return ("uint8", 1, "data[o]")
                return None
            if body.get("kind") == "struct":
                size = fixed_size(node, nodes)
                if size is None:
                    return None
                child = self.emit_struct(prefix, name, body.get("fields") or [], nodes)
                return (child, size, f"decodeNested:{child}")
        return None

    def emit_struct(self, prefix: str, name: str, fields, nodes) -> str:
        struct_name = f"{prefix}{go_name(name)}"
        if struct_name in self.structs:
            return struct_name

        self.structs[struct_name] = ""  # reserve, guards recursion
        self.order.append(struct_name)

        offsets, _sizes = field_offsets(fields, nodes)
        lines_fields: List[str] = []
        lines_decode: List[str] = []
        for f in fields:
            raw = f["name"] if isinstance(f, dict) else None
            if raw is None:
                continue
            key = to_snake(raw)
            if key not in offsets:
                break  # first variable-length field: nothing after it is fixed
            got = self.resolve(f["type"], nodes, prefix)
            if got is None:
                break
            gotype, width, expr = got
            off = offsets[key]
            field_name = go_name(raw)
            lines_fields.append(f"\t{field_name} {gotype} // offset {off}")
            lines_decode.append(
                f'\tif o = base + {off}; o+{width} > len(data) {{\n'
                f'\t\treturn "{key}"\n'
                f"\t}}"
            )
            if expr.startswith("decodeNested:"):
                lines_decode.append(f"\ts.{field_name}.decodeAt(data, o)")
            elif expr.startswith("decodeArray"):
                elem_kind = expr.split(":", 1)[1]
                count = int(gotype[1 : gotype.index("]")])
                elem = gotype[gotype.index("]") + 1 :]
                per = width // count
                assign = (
                    f"s.{field_name}[i].decodeAt(data, o)"
                    if elem_kind == "struct"
                    else f"s.{field_name}[i] = {self._elem_expr(elem)}"
                )
                lines_decode.append(
                    f"\tfor i := 0; i < {count}; i++ {{\n"
                    f"\t\to = base + {off} + i*{per}\n"
                    f"\t\t{assign}\n"
                    f"\t}}"
                )
            else:
                lines_decode.append(f"\ts.{field_name} = {expr}")

        body = "type " + struct_name + " struct {\n" + "\n".join(lines_fields) + "\n}\n\n"
        body += (
            f"// decodeAt fills s from data starting at base, and returns the name of\n"
            f"// the first field the buffer was too short to reach (\"\" if complete).\n"
            f"func (s *{struct_name}) decodeAt(data []byte, base int) string {{\n"
            f"\tvar o int\n"
            f"\t_ = o\n" + "\n".join(lines_decode) + '\n\treturn ""\n}\n\n'
        )
        self.structs[struct_name] = body
        return struct_name

    @staticmethod
    def _elem_expr(elem: str) -> str:
        for key, (gotype, _w, expr) in SCALARS.items():
            if gotype == elem:
                return expr
        return "0"


def main() -> None:
    gen = Generator()
    seen_files = {}

    for spec in LAUNCHPADS:
        if not spec.idl_file:
            continue
        path = IDL_DIR / spec.idl_file
        if not path.exists():
            continue
        doc = seen_files.setdefault(spec.idl_file, json.loads(path.read_text()))
        nodes: Dict[str, dict] = {}
        for section in ("types", "accounts", "events"):
            for node in doc.get(section) or []:
                if node.get("name") and "type" in node:
                    nodes.setdefault(node["name"], node)
        for node in doc.get("types") or []:
            if node.get("name") and "type" in node:
                nodes[node["name"]] = node

        prefix = go_name(spec.key)
        wanted = [spec.state_account] + [c.account_type for c in spec.config_accounts]
        discriminators = {
            entry["name"]: bytes(entry["discriminator"])
            if entry.get("discriminator")
            else account_discriminator(entry["name"])
            for entry in doc.get("accounts") or []
            if entry.get("name")
        }
        for account in wanted:
            if not account or account not in nodes:
                continue
            body = nodes[account]["type"]
            if body.get("kind") != "struct":
                continue
            struct_name = gen.emit_struct(prefix, account, body.get("fields") or [], nodes)
            disc = discriminators.get(account) or account_discriminator(account)
            gen.accounts.append((struct_name, spec.program_id, disc))

    out = [
        "// Code generated by scripts/gen_go.py from the bundled IDLs. DO NOT EDIT.",
        "//",
        "// Only the fixed-width prefix of each account is represented: every field up",
        "// to the first variable-length one has a stable byte offset, and a launch's",
        "// numbers all live in that prefix. Decoding stops cleanly when the buffer is",
        "// shorter than the layout, which is the normal case for accounts written by",
        "// an older build of the program.",
        "",
        "package launchpad",
        "",
        "import (",
        '\t"encoding/binary"',
        '\t"math"',
        ")",
        "",
        "var _ = math.Float64frombits",
        "",
    ]
    for name in gen.order:
        out.append(gen.structs[name])

    out.append("// Discriminators, as the programs write them.")
    out.append("//")
    out.append("// These are NOT globally unique. Anchor derives them from the struct name")
    out.append('// alone, so every program with a `GlobalConfig` or a `Pool` shares the same')
    out.append("// eight bytes -- PumpSwap and Raydium LaunchLab genuinely collide here, as do")
    out.append("// PumpSwap and Vertigo. An account can only be identified by the pair")
    out.append("// (owning program, discriminator), which is what accountRegistry keys on.")
    out.append("var (")
    for struct_name, _program, disc in gen.accounts:
        literal = ", ".join(str(b) for b in disc)
        out.append(f"\tDisc{struct_name} = [8]byte{{{literal}}}")
    out.append(")")
    out.append("")
    out.append("type accountKey struct {")
    out.append("\tprogram string")
    out.append("\tdisc    [8]byte")
    out.append("}")
    out.append("")
    out.append("// accountRegistry resolves (program, discriminator) to an account type.")
    out.append("var accountRegistry = map[accountKey]AccountKind{")
    for struct_name, program, disc in gen.accounts:
        literal = ", ".join(str(b) for b in disc)
        out.append(f'\t{{"{program}", [8]byte{{{literal}}}}}: Kind{struct_name},')
    out.append("}")
    out.append("")
    out.append("// Account type names, one per generated layout.")
    out.append("const (")
    for struct_name, _program, _disc in gen.accounts:
        out.append(f'\tKind{struct_name} AccountKind = "{struct_name}"')
    out.append(")")
    out.append("")
    out.append("// ProgramOf maps each generated account type to its owning program.")
    out.append("var ProgramOf = map[AccountKind]string{")
    for struct_name, program, _disc in gen.accounts:
        out.append(f'\tKind{struct_name}: "{program}",')
    out.append("}")
    out.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out))
    print(f"wrote {OUT.relative_to(ROOT)}: {len(gen.order)} structs, {len(gen.accounts)} accounts")


if __name__ == "__main__":
    main()
