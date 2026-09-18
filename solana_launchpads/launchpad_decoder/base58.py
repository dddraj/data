"""Dependency-free base58 (Bitcoin alphabet) codec for Solana pubkeys."""

from __future__ import annotations

ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = bytearray()
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(ALPHABET[rem])
    for byte in raw:
        if byte != 0:
            break
        out.append(ALPHABET[0])
    return bytes(reversed(out)).decode("ascii")


def b58decode(text: str) -> bytes:
    raw = text.encode("ascii")
    n = 0
    for ch in raw:
        if ch not in _INDEX:
            raise ValueError(f"invalid base58 character {chr(ch)!r} in {text!r}")
        n = n * 58 + _INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for ch in raw:
        if ch != ALPHABET[0]:
            break
        pad += 1
    return b"\x00" * pad + body


def is_pubkey(text: str) -> bool:
    try:
        return len(b58decode(text)) == 32
    except ValueError:
        return False
