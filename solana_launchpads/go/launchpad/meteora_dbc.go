package launchpad

import (
	"fmt"
	"math/big"
)

// Meteora Dynamic Bonding Curve -- program dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN
//
// DBC is the general case, and the one that pays off most for caching. A
// partner (Believe, Jupiter Studio, Bags, ...) creates one PoolConfig describing
// the curve as up to 20 concentrated-liquidity segments -- (sqrt_price,
// liquidity) pairs in Q64.64 -- anchored at sqrt_start_price, plus a
// migration_quote_threshold and the pre/post migration supply. Every VirtualPool
// created against that config then carries only sqrt_price, base_reserve and
// quote_reserve.
//
// So a whole launchpad's static numbers are one account read, shared by all its
// coins:
//
//	launch price     = (sqrt_start_price / 2^64)^2
//	graduation price = (migration_sqrt_price / 2^64)^2
//	supply           = pre_migration_token_supply
//	raise target     = migration_quote_threshold
//
// Build MeteoraDbcParams once per config and reuse it for every pool. Metrics
// then reads direct fields only and allocates nothing; the 20-segment walk,
// which needs 256-bit arithmetic, happens once at construction and only when the
// config leaves migration_sqrt_price at zero.

// Migration targets (PoolConfig.MigrationOption).
const (
	MigrationOptionDammV1 uint8 = 0
	MigrationOptionDammV2 uint8 = 1

	// DBC fee numerators are in 1e-9 units.
	dbcFeeDenominator = 1_000_000_000
)

// MeteoraDbcParams holds everything a pool's own account does not carry.
type MeteoraDbcParams struct {
	QuoteMint     Pubkey
	BaseDecimals  int
	QuoteDecimals int

	SqrtStartPrice     U128
	MigrationSqrtPrice U128
	// MigrationSqrtPriceDerived is true when the config stored zero and the
	// price was walked out of the segments instead.
	MigrationSqrtPriceDerived bool

	SwapBaseAmount          uint64
	MigrationQuoteThreshold uint64
	TotalSupply             uint64
	// TotalSupplyDerived is true when pre_migration_token_supply was zero and
	// the supply was summed from the curve's own parts instead.
	TotalSupplyDerived bool

	MigrationOption uint8
	FeeBps          float64
	Segments        int
	// CurveType is rendered once here rather than per pool, so that pricing a
	// pool allocates nothing.
	CurveType string

	// LaunchPriceRaw and GraduationPriceRaw are quote-raw per base-raw.
	LaunchPriceRaw     float64
	GraduationPriceRaw float64

	// FromChain is false when the params did not come from a PoolConfig
	// account, in which case every static number below is a guess.
	FromChain bool

	// Warnings records anything that looked wrong about the config itself --
	// currently only a swap_base_amount that disagrees with the segments.
	// Config-level, so it is computed once rather than per pool.
	Warnings []Violation
}

// NewMeteoraDbcParams derives the reusable constants from a decoded PoolConfig.
//
// quoteDecimals is 9 for SOL-quoted configs and 6 for USDC; the config does not
// record it, so the caller must supply it from the quote mint. Getting it wrong
// scales every price by a thousand.
func NewMeteoraDbcParams(c *MeteoraDbcPoolConfig, quoteDecimals int) MeteoraDbcParams {
	p := MeteoraDbcParams{
		QuoteMint:               c.QuoteMint,
		BaseDecimals:            int(c.TokenDecimal),
		QuoteDecimals:           quoteDecimals,
		SqrtStartPrice:          c.SqrtStartPrice,
		MigrationSqrtPrice:      c.MigrationSqrtPrice,
		SwapBaseAmount:          c.SwapBaseAmount,
		MigrationQuoteThreshold: c.MigrationQuoteThreshold,
		TotalSupply:             c.PreMigrationTokenSupply,
		MigrationOption:         c.MigrationOption,
		FromChain:               true,
	}

	segments := DbcActiveSegments(&c.Curve)
	p.Segments = len(segments)
	p.CurveType = fmt.Sprintf("piecewise_%d_segment", p.Segments)

	// Older configs leave migration_sqrt_price at zero and expect it to be
	// walked out of the segments against the quote threshold.
	if p.MigrationSqrtPrice.IsZero() && !p.SqrtStartPrice.IsZero() && len(segments) > 0 {
		if sqrt, ok := DbcMigrationSqrtPrice(p.MigrationQuoteThreshold, p.SqrtStartPrice, segments); ok {
			p.MigrationSqrtPrice = sqrt
			p.MigrationSqrtPriceDerived = true
		}
	}

	// Dynamic-supply configs (fixed_token_supply_flag = 0) mint exactly what
	// the curve sells plus what migration needs plus what vesting locks.
	if p.TotalSupply == 0 {
		v := c.LockedVestingConfig
		p.TotalSupply = c.SwapBaseAmount + c.MigrationBaseThreshold +
			v.AmountPerPeriod*v.NumberOfPeriod + v.CliffUnlockAmount
		p.TotalSupplyDerived = true
	}

	p.FeeBps = float64(c.PoolFees.BaseFee.CliffFeeNumerator) / dbcFeeDenominator * 10_000
	p.LaunchPriceRaw = SqrtPriceToRawPrice(p.SqrtStartPrice)
	p.GraduationPriceRaw = SqrtPriceToRawPrice(p.MigrationSqrtPrice)

	// Cross-check the config against itself: walking the segments from the
	// start price to the migration price should reproduce swap_base_amount.
	// A config that misses it was decoded against the wrong layout, which is
	// the failure this catches before it reaches a price.
	if !p.SqrtStartPrice.IsZero() && !p.MigrationSqrtPrice.IsZero() && len(segments) > 0 && p.SwapBaseAmount > 0 {
		derived := DbcBaseForSwap(p.SqrtStartPrice, p.MigrationSqrtPrice, segments)
		tolerance := p.SwapBaseAmount / 1000
		if tolerance < 1 {
			tolerance = 1
		}
		if diffU64(derived, p.SwapBaseAmount) > tolerance {
			p.Warnings = append(p.Warnings, Violation{
				Name: "swap_base_amount_disagrees_with_curve",
				Detail: fmt.Sprintf(
					"swap_base_amount is %d but walking the %d curve segments from the "+
						"start price to the migration price sells %d (>0.1%% apart); the "+
						"config did not decode against the layout it was written with",
					p.SwapBaseAmount, len(segments), derived),
				Severity: SeverityWarning,
			})
		}
	}

	return p
}

// MigrationTarget names the AMM the curve graduates into.
func (p MeteoraDbcParams) MigrationTarget() string {
	switch p.MigrationOption {
	case MigrationOptionDammV1:
		return "damm_v1"
	case MigrationOptionDammV2:
		return "damm_v2"
	}
	return fmt.Sprintf("unknown(%d)", p.MigrationOption)
}

// Metrics turns one decoded VirtualPool into the unified answer.
//
// truncatedAt is what DecodeAccount reported; pass "" when the account decoded
// in full.
func (p MeteoraDbcParams) Metrics(pool *MeteoraDbcVirtualPool, truncatedAt string) Metrics {
	baseDec, quoteDec := p.BaseDecimals, p.QuoteDecimals

	paramsSource := SourceOnchainConfig
	if !p.FromChain {
		paramsSource = SourceFallback
	}

	m := Metrics{
		Launchpad:     "meteora_dbc",
		ProgramID:     MeteoraDbcProgramID,
		Family:        FamilySqrtPiecewise,
		CurveType:     p.CurveType,
		BaseMint:      pool.BaseMint,
		QuoteMint:     p.QuoteMint,
		BaseDecimals:  baseDec,
		QuoteDecimals: quoteDec,
		TotalSupply:   UIAmount(p.TotalSupply, baseDec),
		TokensForSale: UIAmount(p.SwapBaseAmount, baseDec),
		RaiseTarget:   UIAmount(p.MigrationQuoteThreshold, quoteDec),
		Raised:        UIAmount(pool.QuoteReserve, quoteDec),
		Complete:      p.MigrationQuoteThreshold > 0 && pool.QuoteReserve >= p.MigrationQuoteThreshold,
		Migrated:      pool.IsMigrated != 0,
		FeeBps:        p.FeeBps,
		PriceSource:   SourceOnchainState,
		ParamsSource:  paramsSource,
		TruncatedAt:   truncatedAt,
	}
	m.LaunchPrice = PriceUI(p.LaunchPriceRaw, baseDec, quoteDec)
	m.GraduationPrice = PriceUI(p.GraduationPriceRaw, baseDec, quoteDec)
	m.CurrentPrice = PriceUI(SqrtPriceToRawPrice(pool.SqrtPrice), baseDec, quoteDec)

	// The pool holds what it has not sold yet, so what it has sold is the
	// difference from the amount it was funded with.
	if p.SwapBaseAmount > pool.BaseReserve {
		m.TokensSold = UIAmount(p.SwapBaseAmount-pool.BaseReserve, baseDec)
	}

	// Config-level warnings ride along with every pool on that config; the
	// price-level check is this pool's own. Only the latter can be an error,
	// so only it downgrades the price.
	m.Violations = append(m.Violations, p.Warnings...)
	m.Violations = append(m.Violations,
		CheckDbcSqrtPrice(pool.SqrtPrice, p.SqrtStartPrice, p.MigrationSqrtPrice, m.Complete)...)
	if !m.OK() {
		m.PriceSource = "SUSPECT"
	}

	m.fillDerived()
	return m
}

// CheckDbcSqrtPrice tests a pool's price against the range its config allows.
//
// DBC's curve is one-way while it is funding: the price starts at
// sqrt_start_price, only ever rises, and stops at sqrt_migration_price. A pool
// outside that band was either decoded against the wrong config -- easy to do,
// since the config address is a field on the pool and one program serves many
// launchpads -- or its price and its config came from different reads.
func CheckDbcSqrtPrice(sqrtNow, sqrtStart, sqrtMigration U128, complete bool) []Violation {
	if sqrtNow.IsZero() || sqrtStart.IsZero() {
		return nil
	}
	var out []Violation
	if sqrtNow.Cmp(sqrtStart) < 0 {
		out = append(out, Violation{
			Name: "sqrt_price_below_start",
			Detail: fmt.Sprintf(
				"pool sqrt price %.6e is below the config's sqrt_start_price %.6e; a "+
					"funding curve cannot trade below its opening, so the pool and the "+
					"config did not come from one read",
				sqrtNow.Float(), sqrtStart.Float()),
			Severity: SeverityError,
		})
	}
	if !complete && !sqrtMigration.IsZero() && sqrtNow.Cmp(sqrtMigration) > 0 {
		out = append(out, Violation{
			Name: "sqrt_price_above_migration",
			Detail: fmt.Sprintf(
				"pool sqrt price %.6e is past the migration price %.6e while the pool is "+
					"still funding; the curve stops there",
				sqrtNow.Float(), sqrtMigration.Float()),
			Severity: SeverityError,
		})
	}
	return out
}

// --------------------------------------------------------------------------
// the segment walk
//
// Off the hot path: reached once per config, never per pool. Liquidity and
// sqrt prices are both 128-bit and their product is 256-bit, so this uses
// big.Int rather than a hand-rolled wide multiply that would have to get the
// overflow corners right for no measurable gain.
// --------------------------------------------------------------------------

// DbcSegment is one (sqrt_price, liquidity) pair off a config's curve.
type DbcSegment struct {
	SqrtPrice U128
	Liquidity U128
}

// DbcActiveSegments trims the fixed-size 20-slot curve array to its populated
// prefix. A zero price or zero liquidity terminates the curve, exactly as the
// program's own loops treat it.
func DbcActiveSegments(curve *[20]MeteoraDbcLiquidityDistributionConfig) []DbcSegment {
	out := make([]DbcSegment, 0, len(curve))
	for i := range curve {
		if curve[i].SqrtPrice.IsZero() || curve[i].Liquidity.IsZero() {
			break
		}
		out = append(out, DbcSegment{SqrtPrice: curve[i].SqrtPrice, Liquidity: curve[i].Liquidity})
	}
	return out
}

// DbcBaseForSwap is the base tokens the curve sells moving from the start price
// to the migration price -- a port of get_base_token_for_swap in the program.
//
//	Δbase = ⌈ L * (√Pu - √Pl) / (√Pu * √Pl) ⌉
func DbcBaseForSwap(sqrtStart, sqrtMigration U128, curve []DbcSegment) uint64 {
	total := new(big.Int)
	lower := u128Big(sqrtStart)
	migration := u128Big(sqrtMigration)

	for _, segment := range curve {
		upper := u128Big(segment.SqrtPrice)
		liquidity := u128Big(segment.Liquidity)
		if upper.Cmp(migration) > 0 {
			total.Add(total, dbcDeltaBase(lower, migration, liquidity))
			break
		}
		total.Add(total, dbcDeltaBase(lower, upper, liquidity))
		lower = upper
	}
	if !total.IsUint64() {
		return 0
	}
	return total.Uint64()
}

// DbcMigrationSqrtPrice is the price reached once migration_quote_threshold has
// been paid in -- a port of get_migration_threshold_price, used when a config
// stores migration_sqrt_price as zero.
//
// Returns false when the curve's segments cannot absorb the threshold, which
// means the config is inconsistent rather than merely old.
func DbcMigrationSqrtPrice(threshold uint64, sqrtStart U128, curve []DbcSegment) (U128, bool) {
	if len(curve) == 0 {
		return U128{}, false
	}
	remaining := new(big.Int).SetUint64(threshold)
	current := u128Big(sqrtStart)

	for _, segment := range curve {
		upper := u128Big(segment.SqrtPrice)
		liquidity := u128Big(segment.Liquidity)
		capacity := dbcDeltaQuote(current, upper, liquidity)
		if capacity.Cmp(remaining) > 0 {
			// The threshold runs out inside this segment:
			//   √P_next = √P + Δquote * 2^128 / L
			step := new(big.Int).Lsh(remaining, 128)
			step.Div(step, liquidity)
			return bigU128(step.Add(step, current))
		}
		remaining.Sub(remaining, capacity)
		current = upper
		if remaining.Sign() == 0 {
			return bigU128(current)
		}
	}
	if remaining.Sign() == 0 {
		return bigU128(current)
	}
	return U128{}, false
}

// dbcDeltaBase is ⌈ L * (√Pu - √Pl) / (√Pu * √Pl) ⌉.
func dbcDeltaBase(lower, upper, liquidity *big.Int) *big.Int {
	if upper.Cmp(lower) <= 0 || liquidity.Sign() <= 0 || lower.Sign() <= 0 {
		return new(big.Int)
	}
	num := new(big.Int).Sub(upper, lower)
	num.Mul(num, liquidity)
	den := new(big.Int).Mul(lower, upper)
	return ceilDiv(num, den)
}

// dbcDeltaQuote is ⌈ L * (√Pu - √Pl) / 2^128 ⌉ -- both operands carry a Q64
// scale, so the product carries Q128.
func dbcDeltaQuote(lower, upper, liquidity *big.Int) *big.Int {
	if upper.Cmp(lower) <= 0 || liquidity.Sign() <= 0 {
		return new(big.Int)
	}
	num := new(big.Int).Sub(upper, lower)
	num.Mul(num, liquidity)
	return ceilDiv(num, new(big.Int).Lsh(big.NewInt(1), 128))
}

func ceilDiv(num, den *big.Int) *big.Int {
	quo, rem := new(big.Int).QuoRem(num, den, new(big.Int))
	if rem.Sign() != 0 {
		quo.Add(quo, big.NewInt(1))
	}
	return quo
}

func u128Big(u U128) *big.Int {
	out := new(big.Int).SetUint64(u.Hi)
	out.Lsh(out, 64)
	return out.Or(out, new(big.Int).SetUint64(u.Lo))
}

// bigU128 narrows back to 128 bits, reporting false on overflow rather than
// wrapping -- a wrapped sqrt price would be a plausible-looking wrong answer.
func bigU128(v *big.Int) (U128, bool) {
	if v.Sign() < 0 || v.BitLen() > 128 {
		return U128{}, false
	}
	lo := new(big.Int).And(v, new(big.Int).SetUint64(^uint64(0)))
	hi := new(big.Int).Rsh(v, 64)
	return U128{Lo: lo.Uint64(), Hi: hi.Uint64()}, true
}

func diffU64(a, b uint64) uint64 {
	if a > b {
		return a - b
	}
	return b - a
}
