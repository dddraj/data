// Package launchpad decodes Solana bonding-curve launch state into prices,
// supply, market caps and raise targets.
//
// It is a port of the Python reference implementation in this repository, kept
// deliberately narrow: only what belongs in a production ingest or serving
// path. Research tooling -- triaging unknown creator programs, auditing stored
// rows, checking bundled layouts against a cluster -- stays on the Python side
// and runs out of band.
//
// The account layouts in layouts_gen.go are generated from the same IDL
// snapshots the Python side uses, so the two cannot drift apart silently.
// Regenerate with `python scripts/gen_go.py`.
//
// Nothing here allocates per curve. Config-derived constants (the raise target,
// the opening price, k) are computed once into a Params value and reused.
package launchpad

import (
	"math/bits"
)

// Pubkey is a raw 32-byte account address. Kept as an array rather than a
// string so decoding a curve allocates nothing.
type Pubkey [32]byte

var zeroPubkey Pubkey

// IsZero reports whether the key is the default/system address, which several
// programs use to mean "unset".
func (p Pubkey) IsZero() bool { return p == zeroPubkey }

const base58Alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

// String renders the key in base58. This allocates, so keep it off hot paths.
func (p Pubkey) String() string {
	var digits [64]byte
	length := 0
	for i := 0; i < len(p); i++ {
		carry := int(p[i])
		for j := 0; j < length; j++ {
			carry += int(digits[j]) << 8
			digits[j] = byte(carry % 58)
			carry /= 58
		}
		for carry > 0 {
			digits[length] = byte(carry % 58)
			length++
			carry /= 58
		}
	}
	leading := 0
	for leading < len(p) && p[leading] == 0 {
		leading++
	}
	out := make([]byte, 0, leading+length)
	for i := 0; i < leading; i++ {
		out = append(out, base58Alphabet[0])
	}
	for i := length - 1; i >= 0; i-- {
		out = append(out, base58Alphabet[digits[i]])
	}
	return string(out)
}

// ParsePubkey decodes a base58 address.
func ParsePubkey(s string) (Pubkey, error) {
	var out Pubkey
	var buf [64]byte
	length := 0
	for i := 0; i < len(s); i++ {
		index := -1
		for j := 0; j < len(base58Alphabet); j++ {
			if base58Alphabet[j] == s[i] {
				index = j
				break
			}
		}
		if index < 0 {
			return out, &DecodeError{Reason: "invalid base58 character in address"}
		}
		carry := index
		for j := 0; j < length; j++ {
			carry += int(buf[j]) * 58
			buf[j] = byte(carry)
			carry >>= 8
		}
		for carry > 0 {
			buf[length] = byte(carry)
			length++
			carry >>= 8
		}
	}
	leading := 0
	for leading < len(s) && s[leading] == base58Alphabet[0] {
		leading++
	}
	total := leading + length
	if total != 32 {
		return out, &DecodeError{Reason: "address is not 32 bytes"}
	}
	for i := 0; i < length; i++ {
		out[leading+length-1-i] = buf[i]
	}
	return out, nil
}

// U128 is a little-endian 128-bit unsigned integer, as Solana programs store
// sqrt prices and liquidity.
type U128 struct {
	Lo uint64
	Hi uint64
}

// Float converts to float64. Exact below 2^53; beyond that it carries enough
// precision for ratio comparisons, which is all it is used for.
func (u U128) Float() float64 {
	return float64(u.Hi)*18446744073709551616.0 + float64(u.Lo)
}

// IsZero reports whether both halves are zero.
func (u U128) IsZero() bool { return u.Lo == 0 && u.Hi == 0 }

// Cmp returns -1, 0 or 1.
func (u U128) Cmp(other U128) int {
	switch {
	case u.Hi != other.Hi:
		if u.Hi < other.Hi {
			return -1
		}
		return 1
	case u.Lo != other.Lo:
		if u.Lo < other.Lo {
			return -1
		}
		return 1
	}
	return 0
}

// MulU64 multiplies two uint64s without overflowing, which matters because a
// curve's k is around 3e25 and a uint64 tops out near 1.8e19.
func MulU64(a, b uint64) U128 {
	hi, lo := bits.Mul64(a, b)
	return U128{Lo: lo, Hi: hi}
}

// DecodeError describes why an account could not be decoded.
type DecodeError struct {
	Reason string
}

func (e *DecodeError) Error() string { return e.Reason }

func u128At(data []byte, o int) U128 {
	return U128{
		Lo: leU64(data[o:]),
		Hi: leU64(data[o+8:]),
	}
}

func pubkeyAt(data []byte, o int) Pubkey {
	var out Pubkey
	copy(out[:], data[o:o+32])
	return out
}

func leU64(b []byte) uint64 {
	_ = b[7]
	return uint64(b[0]) | uint64(b[1])<<8 | uint64(b[2])<<16 | uint64(b[3])<<24 |
		uint64(b[4])<<32 | uint64(b[5])<<40 | uint64(b[6])<<48 | uint64(b[7])<<56
}
