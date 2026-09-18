package launchpad

import "math/big"

// Curve mathematics, in raw on-chain units, matching the programs' own
// rounding. See docs/CURVE_MATH.md for the derivations.

// PriceUI converts a raw price (quote base-units per base base-unit) into whole
// quote per whole token. For a 6-decimal token quoted in SOL that is a factor
// of 1e-3, and getting it wrong is the classic thousand-fold error.
func PriceUI(rawPrice float64, baseDecimals, quoteDecimals int) float64 {
	return rawPrice * pow10(baseDecimals-quoteDecimals)
}

// UIAmount converts raw base units to whole tokens.
func UIAmount(raw uint64, decimals int) float64 {
	return float64(raw) * pow10(-decimals)
}

func pow10(exp int) float64 {
	out := 1.0
	if exp >= 0 {
		for i := 0; i < exp; i++ {
			out *= 10
		}
		return out
	}
	for i := 0; i < -exp; i++ {
		out /= 10
	}
	return out
}

// CPPriceRaw is the spot price of a constant-product curve: vQuote / vBase.
func CPPriceRaw(virtualQuote, virtualBase uint64) float64 {
	if virtualBase == 0 {
		return 0
	}
	return float64(virtualQuote) / float64(virtualBase)
}

// CPBaseOut is the tokens received for quoteIn, floored exactly as the
// programs floor it.
func CPBaseOut(virtualQuote, virtualBase, quoteIn uint64) uint64 {
	if quoteIn == 0 {
		return 0
	}
	num := new(big.Int).Mul(new(big.Int).SetUint64(quoteIn), new(big.Int).SetUint64(virtualBase))
	den := new(big.Int).SetUint64(virtualQuote + quoteIn)
	return num.Div(num, den).Uint64()
}

// CPQuoteIn is the quote needed to buy exactly baseOut tokens, rounded up.
// Returns false when baseOut would drain the whole virtual reserve.
func CPQuoteIn(virtualQuote, virtualBase, baseOut uint64) (uint64, bool) {
	if baseOut == 0 {
		return 0, true
	}
	if baseOut >= virtualBase {
		return 0, false
	}
	num := new(big.Int).Mul(new(big.Int).SetUint64(virtualQuote), new(big.Int).SetUint64(baseOut))
	den := new(big.Int).SetUint64(virtualBase - baseOut)
	quo, rem := new(big.Int).QuoRem(num, den, new(big.Int))
	if rem.Sign() != 0 {
		quo.Add(quo, big.NewInt(1))
	}
	if !quo.IsUint64() {
		return 0, false
	}
	return quo.Uint64(), true
}

// CPRaiseForSupply is the gross quote (before fees) needed to drain baseForSale
// off the curve -- the bonding-curve raise target.
func CPRaiseForSupply(initialVirtualQuote, initialVirtualBase, baseForSale uint64) (uint64, bool) {
	if baseForSale == 0 || baseForSale >= initialVirtualBase {
		return 0, false
	}
	return CPQuoteIn(initialVirtualQuote, initialVirtualBase, baseForSale)
}

// CPFinalPriceRaw is the spot price once the curve is exhausted.
func CPFinalPriceRaw(initialVirtualQuote, initialVirtualBase, baseForSale uint64) (float64, bool) {
	if baseForSale >= initialVirtualBase {
		return 0, false
	}
	raised, ok := CPRaiseForSupply(initialVirtualQuote, initialVirtualBase, baseForSale)
	if !ok {
		return 0, false
	}
	return float64(initialVirtualQuote+raised) / float64(initialVirtualBase-baseForSale), true
}

// LaunchLabLinearPriceRaw is Raydium LaunchLab's linear curve: the slope lives
// in virtual_base as a Q64 value and real_base is tokens sold.
func LaunchLabLinearPriceRaw(slopeQ64, baseSold uint64) float64 {
	if slopeQ64 == 0 {
		return 0
	}
	product := MulU64(slopeQ64, baseSold)
	return product.Float() / 18446744073709551616.0
}

// SqrtPriceToRawPrice converts a Q64.64 sqrt price to a raw price, as Meteora
// DBC stores it: (sqrtP / 2^64)^2.
func SqrtPriceToRawPrice(sqrtPrice U128) float64 {
	if sqrtPrice.IsZero() {
		return 0
	}
	ratio := sqrtPrice.Float() / 18446744073709551616.0
	return ratio * ratio
}

// Progress clamps raised/target into [0, 1].
func Progress(raised, target float64) float64 {
	if target <= 0 {
		return 0
	}
	ratio := raised / target
	switch {
	case ratio < 0:
		return 0
	case ratio > 1:
		return 1
	}
	return ratio
}
