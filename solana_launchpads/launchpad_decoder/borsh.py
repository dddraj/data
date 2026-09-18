"""Minimal Borsh reader used by the IDL-driven account decoder.

Only the subset of Borsh that Anchor emits in account state is implemented:
fixed-width integers, bools, floats, pubkeys, strings, byte vectors, vectors,
fixed arrays, options and nested structs/enums.
"""

from __future__ import annotations

import struct

from .base58 import b58encode


class BorshError(ValueError):
    """Raised when a buffer is too short or holds an impossible value."""


class BorshReader:
    __slots__ = ("buf", "offset")

    def __init__(self, buf: bytes, offset: int = 0) -> None:
        self.buf = buf
        self.offset = offset

    @property
    def remaining(self) -> int:
        return len(self.buf) - self.offset

    def take(self, n: int) -> bytes:
        if n < 0:
            raise BorshError(f"negative read length {n}")
        end = self.offset + n
        if end > len(self.buf):
            raise BorshError(
                f"buffer underrun: need {n} bytes at offset {self.offset}, "
                f"only {self.remaining} left"
            )
        chunk = self.buf[self.offset : end]
        self.offset = end
        return chunk

    def skip(self, n: int) -> None:
        self.take(n)

    # -- scalars ---------------------------------------------------------
    def u8(self) -> int:
        return self.take(1)[0]

    def i8(self) -> int:
        return struct.unpack("<b", self.take(1))[0]

    def _uint(self, size: int) -> int:
        return int.from_bytes(self.take(size), "little", signed=False)

    def _int(self, size: int) -> int:
        return int.from_bytes(self.take(size), "little", signed=True)

    def u16(self) -> int:
        return self._uint(2)

    def u32(self) -> int:
        return self._uint(4)

    def u64(self) -> int:
        return self._uint(8)

    def u128(self) -> int:
        return self._uint(16)

    def u256(self) -> int:
        return self._uint(32)

    def i16(self) -> int:
        return self._int(2)

    def i32(self) -> int:
        return self._int(4)

    def i64(self) -> int:
        return self._int(8)

    def i128(self) -> int:
        return self._int(16)

    def i256(self) -> int:
        return self._int(32)

    def f32(self) -> float:
        return struct.unpack("<f", self.take(4))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.take(8))[0]

    def boolean(self) -> bool:
        raw = self.u8()
        if raw > 1:
            raise BorshError(f"invalid bool byte {raw}")
        return raw == 1

    def pubkey(self) -> str:
        return b58encode(self.take(32))

    def string(self) -> str:
        length = self.u32()
        return self.take(length).decode("utf-8", errors="replace")

    def byte_vec(self) -> bytes:
        return self.take(self.u32())
