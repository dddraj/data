package launchpad

import (
	"fmt"
	"math"
	"math/big"
)

// Self-consistency checks on decoded curve state.
//
// A decoded price can be wrong in two ways. The layout can be wrong, which the
// Python-side selftest catches against a live cluster. Or the values can be
// individually plausible but mutually impossible -- a reserve pair that does
// not lie on the curve it claims to be on. The second kind passes every schema
// check and shows up downstream as a wrong price on a coin that looks fine.
//
// A constant-product curve gives the test for free: vBase*vQuote is invariant.
// Programs floor their outputs, so k creeps *up* by parts per billion over a
// curve's life and never moves down. A pair missing k by a visible margin did
// not come off one account read -- in practice, two fields sourced from
// different places and merged into one row.
//
// One limitation, stated plainly: the opening k comes from the program's config
// as it stands now. If a launchpad ever changed its opening reserves, curves
// created before the change sit on a different k and will be flagged although
// they are correct. That false positive is a cluster of old curves all off by
// the same ratio; mixed fields scatter. Bucketing the ratio tells them apart.

const (
	// KTolerance is far beyond any legitimate drift while staying clear of
	// floating-point noise.
	KTolerance = 0.005
	// OpenTolerance is how far a reserve may sit past its opening value before
	// it is treated as impossible rather than as rounding dust.
	OpenTolerance = 1e-6
)

// Severity distinguishes a wrong number from a stale one.
type Severity string

const (
	SeverityError   Severity = "error"
	SeverityWarning Severity = "warning"
)

// Violation is one broken invariant.
type Violation struct {
	Name     string
	Detail   string
	Severity Severity
}

func (v Violation) String() string { return v.Name + ": " + v.Detail }

// CheckConstantProductPair reports whether a virtual reserve pair lies on the
// curve it claims to be on. Returns nil for a consistent pair, which is the
// common case and allocates nothing.
func CheckConstantProductPair(virtualBase, virtualQuote, initialBase, initialQuote uint64) []Violation {
	if virtualBase == 0 || virtualQuote == 0 || initialBase == 0 || initialQuote == 0 {
		return nil
	}

	kOpen := MulU64(initialBase, initialQuote).Float()
	kNow := MulU64(virtualBase, virtualQuote).Float()
	drift := kNow/kOpen - 1

	var out []Violation
	if drift > KTolerance || drift < -KTolerance {
		out = append(out, Violation{
			Name: "k_violation",
			Detail: fmt.Sprintf(
				"vBase*vQuote is %.6e, the curve opened at %.6e (%+.1f%%). Flooring "+
					"moves k up by parts per billion and never down, so this pair did "+
					"not come from one account read", kNow, kOpen, drift*100),
			Severity: SeverityError,
		})
	}
	if float64(virtualQuote) < float64(initialQuote)*(1-OpenTolerance) {
		out = append(out, Violation{
			Name: "quote_below_open",
			Detail: fmt.Sprintf("virtual quote %d is below the opening %d; a one-way "+
				"curve cannot go below its seed", virtualQuote, initialQuote),
			Severity: SeverityError,
		})
	}
	if float64(virtualBase) > float64(initialBase)*(1+OpenTolerance) {
		out = append(out, Violation{
			Name: "base_above_open",
			Detail: fmt.Sprintf("virtual base %d is above the opening %d; more tokens "+
				"would have been sold into the curve than ever left it", virtualBase, initialBase),
			Severity: SeverityError,
		})
	}
	return out
}

// DiagnosePair names the likelier culprit in an inconsistent pair.
//
// Each reserve has a legal range on a one-way curve: the base only ever falls
// from its opening, the quote only ever rises. Whichever observed value sits
// outside its range is the one to distrust, and the other implies what it
// should have been. When both are out, the one that has travelled further past
// its bound is named first.
//
// Returns an empty field name when the pair is consistent or cannot be judged.
func DiagnosePair(virtualBase, virtualQuote, initialBase, initialQuote, baseForSale uint64) (string, string) {
	if virtualBase == 0 || virtualQuote == 0 || initialBase == 0 || initialQuote == 0 {
		return "", "insufficient data"
	}

	kOpen := MulU64(initialBase, initialQuote)
	drift := MulU64(virtualBase, virtualQuote).Float()/kOpen.Float() - 1
	if drift <= KTolerance && drift >= -KTolerance {
		return "", "pair is consistent with the opening k"
	}

	impliedQuote := divU128ByU64(kOpen, virtualBase)
	impliedBase := divU128ByU64(kOpen, virtualQuote)

	var floorBase uint64
	if baseForSale > 0 && baseForSale < initialBase {
		floorBase = initialBase - baseForSale
	}
	baseOK := virtualBase >= floorBase && float64(virtualBase) <= float64(initialBase)*(1+OpenTolerance)
	quoteOK := float64(virtualQuote) >= float64(initialQuote)*(1-OpenTolerance)

	switch {
	case baseOK && !quoteOK:
		return "virtual_quote", fmt.Sprintf(
			"virtual base is in range, virtual quote is not; on this base the quote "+
				"should be %d (%.4f in 9-decimal units), not %d",
			impliedQuote, float64(impliedQuote)/1e9, virtualQuote)
	case quoteOK && !baseOK:
		return "virtual_base", fmt.Sprintf(
			"virtual quote is in range, virtual base is not; on this quote the base "+
				"should be %d, not %d", impliedBase, virtualBase)
	case !baseOK && !quoteOK:
		baseExcursion := 0.0
		if virtualBase > initialBase {
			baseExcursion = float64(virtualBase-initialBase) / float64(initialBase)
		}
		quoteExcursion := 0.0
		if virtualQuote < initialQuote {
			quoteExcursion = float64(initialQuote-virtualQuote) / float64(initialQuote)
		}
		if quoteExcursion >= baseExcursion {
			return "virtual_quote", fmt.Sprintf(
				"both reserves are out of range, but the quote is further out (%.1f%% "+
					"below its opening vs %.1f%% above for the base); on this base the "+
					"quote should be %d (%.4f in 9-decimal units), not %d",
				quoteExcursion*100, baseExcursion*100, impliedQuote,
				float64(impliedQuote)/1e9, virtualQuote)
		}
		return "virtual_base", fmt.Sprintf(
			"both reserves are out of range, but the base is further out (%.1f%% above "+
				"its opening vs %.1f%% below for the quote); on this quote the base "+
				"should be %d, not %d",
			baseExcursion*100, quoteExcursion*100, impliedBase, virtualBase)
	}
	return "", "both reserves are individually in range but their product is not k"
}

// divU128ByU64 floors a 128-bit value by a 64-bit divisor.
//
// Only reached once a violation has already been found, so clarity beats
// speed: big.Int has no overflow corner to get wrong, unlike a hand-rolled
// long division whose remainder can overflow for a large divisor.
func divU128ByU64(value U128, divisor uint64) uint64 {
	if divisor == 0 {
		return 0
	}
	if value.Hi == 0 {
		return value.Lo / divisor
	}
	n := new(big.Int).Lsh(new(big.Int).SetUint64(value.Hi), 64)
	n.Or(n, new(big.Int).SetUint64(value.Lo))
	n.Div(n, new(big.Int).SetUint64(divisor))
	if !n.IsUint64() {
		return 0
	}
	return n.Uint64()
}

// VariantCandidates maps a variant name to the opening quote reserve pump.fun
// seeds that variant with. SOL-quoted and non-SOL-quoted curves get different
// openings and the account does not say which it is.
type VariantCandidates map[string]uint64

// ClassifyVariant reports which opening constant a curve was seeded with, read
// off the reserves themselves.
//
// pump.fun's buy and sell add the post-fee amount to the virtual *and* the real
// quote reserve together, so their difference never moves:
//
//	virtual_quote - real_quote == initial_virtual_quote_reserves
//
// for the whole life of the curve. That makes the difference an exact
// classifier: no tolerance, and it keeps working after a curve has traded well
// away from its opening, which is exactly where counting curves still sitting
// near that opening fails.
//
// Returns the variant name and its constant, or ("", observed offset) when the
// difference matches nothing -- which means the pair did not come from one read.
func ClassifyVariant(virtualQuote, realQuote uint64, candidates VariantCandidates) (string, int64) {
	offset := int64(virtualQuote) - int64(realQuote)
	for name, initial := range candidates {
		if initial != 0 && offset == int64(initial) {
			return name, offset
		}
	}
	return "", offset
}

// CheckReserveOffset flags a curve whose virtual/real quote difference matches
// no opening constant.
//
// An offset near zero is the signature of the virtual column carrying the
// *real* reserve: the two are then the same number, or the real one was
// defaulted away by a later write on the same key.
func CheckReserveOffset(virtualQuote, realQuote uint64, candidates VariantCandidates) []Violation {
	if len(candidates) == 0 {
		return nil
	}
	variant, offset := ClassifyVariant(virtualQuote, realQuote, candidates)
	if variant != "" {
		return nil
	}

	smallest := uint64(math.MaxUint64)
	for _, initial := range candidates {
		if initial != 0 && initial < smallest {
			smallest = initial
		}
	}
	detail := fmt.Sprintf(
		"virtual_quote - real_quote is %d, which matches no opening constant. "+
			"That difference is fixed for a curve's whole life, so this pair did "+
			"not come from one read", offset)
	if abs64(offset) < int64(smallest/2) {
		detail += "; an offset near zero is what a virtual column carrying the REAL reserve looks like"
	}
	return []Violation{{Name: "reserve_offset_mismatch", Detail: detail, Severity: SeverityError}}
}

func abs64(v int64) int64 {
	if v < 0 {
		return -v
	}
	return v
}
