package launchpad

import (
	"math"
	"math/big"
	"testing"
)

// Behaviour the golden vectors cannot cover, because it only exists on the Go
// side: the per-launchpad invariants, and the allocation budget the serving
// path is held to.

// --------------------------------------------------------------------------
// Raydium LaunchLab
// --------------------------------------------------------------------------

func launchlabPool() RaydiumLaunchlabPoolState {
	return RaydiumLaunchlabPoolState{
		BaseDecimals:          6,
		QuoteDecimals:         9,
		Status:                0,
		Supply:                1_000_000_000_000_000,
		TotalBaseSell:         irt,
		VirtualBase:           ivt,
		VirtualQuote:          ivs,
		TotalQuoteFundRaising: 85_000_000_000,
	}
}

func launchlabParams(curveType uint8) RaydiumLaunchlabParams {
	return NewRaydiumLaunchlabParams(&RaydiumLaunchlabGlobalConfig{
		CurveType:    curveType,
		TradeFeeRate: 10_000,
	})
}

func TestLaunchLabCurveTypesNameThemselves(t *testing.T) {
	for curveType, want := range map[uint8]string{
		CurveTypeConstantProduct: "constant_product",
		CurveTypeFixedPrice:      "fixed_price",
		CurveTypeLinear:          "linear",
		250:                      "unknown",
	} {
		if got := (RaydiumLaunchlabParams{CurveType: curveType}).CurveTypeName(); got != want {
			t.Errorf("curve type %d = %q, want %q", curveType, got, want)
		}
	}
}

func TestLaunchLabFixedPriceNeverMoves(t *testing.T) {
	pool := launchlabPool()
	pool.RealBase = irt / 2
	pool.RealQuote = 42_500_000_000

	m := launchlabParams(CurveTypeFixedPrice).Metrics(&pool, "")
	if m.CurrentPrice != m.LaunchPrice {
		t.Errorf("fixed price moved: launch %g, current %g", m.LaunchPrice, m.CurrentPrice)
	}
}

func TestLaunchLabLinearCurveOpensAtZeroAndRises(t *testing.T) {
	// LaunchLab sizes a linear curve as a = 2*raise*2^64 / totalSell^2, with
	// the slope in virtual_base as a u64.
	const raise = 85_000_000_000
	totalSell := uint64(2 * raise * 1_000_000_000_000_000 / (3 * raise))
	slope := new(big.Int).Lsh(big.NewInt(2*raise), 64)
	slope.Div(slope, new(big.Int).Mul(new(big.Int).SetUint64(totalSell), new(big.Int).SetUint64(totalSell)))
	if !slope.IsUint64() {
		t.Fatalf("slope %s does not fit a u64", slope)
	}

	pool := launchlabPool()
	pool.VirtualBase = slope.Uint64()
	pool.TotalBaseSell = totalSell

	params := launchlabParams(CurveTypeLinear)
	if m := params.Metrics(&pool, ""); m.LaunchPrice != 0 {
		t.Errorf("a linear curve opens at zero, got %g", m.LaunchPrice)
	}

	pool.RealBase = totalSell
	sold := params.Metrics(&pool, "")
	// A linear curve ends at twice its average price. The 2% tolerance is the
	// program's, not ours: the slope lands near 7 as a u64 for this raise, so
	// LaunchLab itself quantises it by about a percent.
	want := 2 * PriceUI(float64(raise)/float64(totalSell), 6, 9)
	if math.Abs(sold.CurrentPrice-want)/want > 0.02 {
		t.Errorf("final linear price = %g, want ~%g", sold.CurrentPrice, want)
	}
}

func TestLaunchLabStatusMapsToCompleteAndMigrated(t *testing.T) {
	params := launchlabParams(CurveTypeConstantProduct)
	for _, tc := range []struct {
		status             uint8
		complete, migrated bool
	}{
		{poolStatusFunding, false, false},
		{poolStatusMigrate, true, false},
		{poolStatusMigrated, true, true},
	} {
		pool := launchlabPool()
		pool.Status = tc.status
		m := params.Metrics(&pool, "")
		if m.Complete != tc.complete || m.Migrated != tc.migrated {
			t.Errorf("status %d: complete=%v migrated=%v, want %v/%v",
				tc.status, m.Complete, m.Migrated, tc.complete, tc.migrated)
		}
	}
}

func TestLaunchLabFeeIsTheSumOfThreeRates(t *testing.T) {
	params := launchlabParams(CurveTypeConstantProduct).WithPlatform(
		&RaydiumLaunchlabPlatformConfig{FeeRate: 10_000, CreatorFeeRate: 5_000})
	pool := launchlabPool()
	// 10k + 10k + 5k hundredths of a bip is 2.5%, which is 250 bps.
	if m := params.Metrics(&pool, ""); math.Abs(m.FeeBps-250) > 1e-9 {
		t.Errorf("fee = %g bps, want 250", m.FeeBps)
	}
}

func TestLaunchLabWithoutAConfigSaysSo(t *testing.T) {
	pool := launchlabPool()
	m := RaydiumLaunchlabParams{}.Metrics(&pool, "")
	if m.ParamsSource != SourceFallback {
		t.Errorf("params source = %q, want %q", m.ParamsSource, SourceFallback)
	}
}

func TestLaunchLabHoldsItsVirtualPairToTheOpeningK(t *testing.T) {
	// LaunchLab writes the virtual pair once and never moves it, so the
	// effective product must stay at virtual_base * virtual_quote.
	pool := launchlabPool()
	pool.RealQuote = 42_500_000_000
	pool.RealBase = ivt - divU128ByU64(MulU64(ivt, ivs), ivs+pool.RealQuote)
	if v := CheckLaunchLabConstantProduct(pool.VirtualBase, pool.VirtualQuote, pool.RealBase, pool.RealQuote); len(v) != 0 {
		t.Fatalf("a pool on its own k should not be flagged: %v", v)
	}

	// Now the real reserves come from a different read: the quote moved but
	// the base did not.
	stale := pool
	stale.RealBase = 0
	violations := CheckLaunchLabConstantProduct(
		stale.VirtualBase, stale.VirtualQuote, stale.RealBase, stale.RealQuote)
	if len(violations) == 0 {
		t.Fatal("a mismatched real/virtual pair should be flagged")
	}
	m := launchlabParams(CurveTypeConstantProduct).Metrics(&stale, "")
	if m.OK() || m.PriceSource != "SUSPECT" {
		t.Errorf("a k-violating pool should not publish a price: ok=%v source=%q",
			m.OK(), m.PriceSource)
	}
}

func TestLaunchLabRejectsADrainedVirtualBase(t *testing.T) {
	violations := CheckLaunchLabConstantProduct(ivt, ivs, ivt, 0)
	if len(violations) != 1 || violations[0].Name != "real_base_exceeds_virtual" {
		t.Fatalf("want real_base_exceeds_virtual, got %v", violations)
	}
}

func TestPricingALaunchLabPoolDoesNotAllocate(t *testing.T) {
	params := launchlabParams(CurveTypeConstantProduct)
	pool := launchlabPool()
	allocations := testing.AllocsPerRun(100, func() {
		_ = params.Metrics(&pool, "")
	})
	if allocations != 0 {
		t.Errorf("pricing allocated %.0f times per run, want 0", allocations)
	}
}

// --------------------------------------------------------------------------
// Meteora DBC
// --------------------------------------------------------------------------

func sqrtQ64(price float64) U128 {
	v := new(big.Float).Mul(
		big.NewFloat(math.Sqrt(price)),
		new(big.Float).SetInt(new(big.Int).Lsh(big.NewInt(1), 64)),
	)
	i, _ := v.Int(nil)
	out, ok := bigU128(i)
	if !ok {
		panic("sqrt price does not fit 128 bits")
	}
	return out
}

func dbcConfig() MeteoraDbcPoolConfig {
	return MeteoraDbcPoolConfig{
		TokenDecimal:            6,
		MigrationOption:         MigrationOptionDammV2,
		SqrtStartPrice:          sqrtQ64(1e-5),
		MigrationSqrtPrice:      sqrtQ64(1e-3),
		MigrationQuoteThreshold: 85_000_000_000,
		SwapBaseAmount:          800_000_000_000_000,
		MigrationBaseThreshold:  200_000_000_000_000,
		PreMigrationTokenSupply: 1_000_000_000_000_000,
		PoolFees: MeteoraDbcPoolFeesConfig{
			BaseFee: MeteoraDbcBaseFeeConfig{CliffFeeNumerator: 20_000_000},
		},
	}
}

func TestDbcReadsTheWholeLaunchFromItsConfig(t *testing.T) {
	cfg := dbcConfig()
	params := NewMeteoraDbcParams(&cfg, 9)
	pool := MeteoraDbcVirtualPool{
		BaseReserve:  cfg.SwapBaseAmount,
		QuoteReserve: 0,
		SqrtPrice:    cfg.SqrtStartPrice,
	}
	m := params.Metrics(&pool, "")

	for _, c := range []struct {
		field     string
		got, want float64
	}{
		{"launch price", m.LaunchPrice, 1e-8},
		{"graduation price", m.GraduationPrice, 1e-6},
		{"total supply", m.TotalSupply, 1e9},
		{"raise target", m.RaiseTarget, 85},
		{"launch mcap", m.LaunchMcap, 10},
		{"fee bps", m.FeeBps, 200}, // 2e7 / 1e9 is 2%
	} {
		if math.Abs(c.got-c.want)/c.want > 1e-6 {
			t.Errorf("%s = %g, want %g", c.field, c.got, c.want)
		}
	}
	if params.MigrationTarget() != "damm_v2" {
		t.Errorf("migration target = %q, want damm_v2", params.MigrationTarget())
	}
	if m.Complete || m.Migrated {
		t.Error("a fresh pool is neither complete nor migrated")
	}
}

func TestDbcCompletesOnTheQuoteThreshold(t *testing.T) {
	cfg := dbcConfig()
	params := NewMeteoraDbcParams(&cfg, 9)
	pool := MeteoraDbcVirtualPool{
		QuoteReserve: cfg.MigrationQuoteThreshold,
		SqrtPrice:    cfg.MigrationSqrtPrice,
		IsMigrated:   1,
	}
	m := params.Metrics(&pool, "")
	if !m.Complete || !m.Migrated {
		t.Errorf("complete=%v migrated=%v, want both true", m.Complete, m.Migrated)
	}
	if m.Progress != 1 {
		t.Errorf("progress = %g, want 1", m.Progress)
	}
}

func TestDbcDerivesADynamicSupply(t *testing.T) {
	cfg := dbcConfig()
	cfg.PreMigrationTokenSupply = 0
	cfg.LockedVestingConfig = MeteoraDbcLockedVestingConfig{
		AmountPerPeriod:   10_000_000_000_000,
		NumberOfPeriod:    10,
		CliffUnlockAmount: 5_000_000_000_000,
	}
	params := NewMeteoraDbcParams(&cfg, 9)
	if !params.TotalSupplyDerived {
		t.Error("a zero pre_migration_token_supply should be derived, not reported as zero")
	}
	want := cfg.SwapBaseAmount + cfg.MigrationBaseThreshold + 10*10_000_000_000_000 + 5_000_000_000_000
	if params.TotalSupply != want {
		t.Errorf("derived supply = %d, want %d", params.TotalSupply, want)
	}
}

func TestDbcWalksTheMigrationPriceOutOfItsSegments(t *testing.T) {
	cfg := dbcConfig()
	cfg.MigrationSqrtPrice = U128{}
	cfg.Curve[0] = MeteoraDbcLiquidityDistributionConfig{
		SqrtPrice: sqrtQ64(1e-4),
		Liquidity: bigMust(t, "200000000000000000000000000000000"), // 2e32
	}
	cfg.Curve[1] = MeteoraDbcLiquidityDistributionConfig{
		SqrtPrice: sqrtQ64(1e-3),
		Liquidity: bigMust(t, "1000000000000000000000000000000000"), // 1e33
	}
	segments := DbcActiveSegments(&cfg.Curve)
	if len(segments) != 2 {
		t.Fatalf("active segments = %d, want 2", len(segments))
	}
	cfg.SwapBaseAmount = 0 // do not trigger the cross-check

	params := NewMeteoraDbcParams(&cfg, 9)
	if !params.MigrationSqrtPriceDerived {
		t.Fatal("a zero migration_sqrt_price should be walked out of the segments")
	}
	// The threshold overflows the first segment, so the price must land inside
	// the second: above its lower bound and at or below its own.
	if params.MigrationSqrtPrice.Cmp(cfg.Curve[0].SqrtPrice) <= 0 {
		t.Errorf("migration price %v did not clear the first segment", params.MigrationSqrtPrice)
	}
	if params.MigrationSqrtPrice.Cmp(cfg.Curve[1].SqrtPrice) > 0 {
		t.Errorf("migration price %v ran past the last segment", params.MigrationSqrtPrice)
	}

	// And walking base out to that price must reproduce what a config would
	// have stored as swap_base_amount.
	if base := DbcBaseForSwap(cfg.SqrtStartPrice, params.MigrationSqrtPrice, segments); base == 0 {
		t.Error("walking the segments sold no base at all")
	}
}

func TestDbcFlagsASwapBaseAmountThatDisagreesWithTheCurve(t *testing.T) {
	cfg := dbcConfig()
	cfg.Curve[0] = MeteoraDbcLiquidityDistributionConfig{
		SqrtPrice: sqrtQ64(1e-3),
		Liquidity: bigMust(t, "200000000000000000000000000000000"),
	}
	cfg.SwapBaseAmount = 1 // nowhere near what the segments sell

	params := NewMeteoraDbcParams(&cfg, 9)
	if len(params.Warnings) != 1 || params.Warnings[0].Name != "swap_base_amount_disagrees_with_curve" {
		t.Fatalf("want swap_base_amount_disagrees_with_curve, got %v", params.Warnings)
	}
	// A config-level warning travels with every pool on that config, but it is
	// not an error: the pool's own price is still worth publishing.
	pool := MeteoraDbcVirtualPool{SqrtPrice: cfg.SqrtStartPrice}
	m := params.Metrics(&pool, "")
	if !m.OK() {
		t.Error("a config warning should not suppress the pool's price")
	}
	if len(m.Violations) != 1 {
		t.Errorf("the warning should reach the metrics, got %v", m.Violations)
	}
}

func TestDbcRejectsAPriceOutsideItsConfiguredBand(t *testing.T) {
	cfg := dbcConfig()
	params := NewMeteoraDbcParams(&cfg, 9)

	below := MeteoraDbcVirtualPool{SqrtPrice: sqrtQ64(1e-7)}
	if m := params.Metrics(&below, ""); m.OK() || m.PriceSource != "SUSPECT" {
		t.Errorf("a price below sqrt_start_price should be suspect: %v", m.Violations)
	}

	above := MeteoraDbcVirtualPool{SqrtPrice: sqrtQ64(1e-1)}
	if m := params.Metrics(&above, ""); m.OK() {
		t.Errorf("a funding pool past its migration price should be suspect: %v", m.Violations)
	}

	// Once the threshold is met the price is allowed to sit at migration.
	done := MeteoraDbcVirtualPool{
		SqrtPrice:    cfg.MigrationSqrtPrice,
		QuoteReserve: cfg.MigrationQuoteThreshold,
	}
	if m := params.Metrics(&done, ""); !m.OK() {
		t.Errorf("a completed pool at its migration price is fine: %v", m.Violations)
	}
}

func TestDbcActiveSegmentsStopsAtTheFirstEmptySlot(t *testing.T) {
	var cfg MeteoraDbcPoolConfig
	cfg.Curve[0] = MeteoraDbcLiquidityDistributionConfig{
		SqrtPrice: sqrtQ64(1e-4), Liquidity: U128{Lo: 1},
	}
	// slot 1 left empty; slot 2 populated but unreachable
	cfg.Curve[2] = MeteoraDbcLiquidityDistributionConfig{
		SqrtPrice: sqrtQ64(1e-3), Liquidity: U128{Lo: 1},
	}
	if got := DbcActiveSegments(&cfg.Curve); len(got) != 1 {
		t.Errorf("active segments = %d, want 1", len(got))
	}
}

func TestDbcMigrationPriceFailsWhenTheCurveCannotAbsorbTheThreshold(t *testing.T) {
	segments := []DbcSegment{{SqrtPrice: sqrtQ64(1e-4), Liquidity: U128{Lo: 1}}}
	if _, ok := DbcMigrationSqrtPrice(85_000_000_000, sqrtQ64(1e-5), segments); ok {
		t.Error("a curve with no liquidity cannot reach the threshold")
	}
	if _, ok := DbcMigrationSqrtPrice(85_000_000_000, sqrtQ64(1e-5), nil); ok {
		t.Error("an empty curve has no migration price")
	}
}

func TestPricingADbcPoolDoesNotAllocate(t *testing.T) {
	cfg := dbcConfig()
	params := NewMeteoraDbcParams(&cfg, 9)
	pool := MeteoraDbcVirtualPool{
		BaseReserve:  cfg.SwapBaseAmount,
		QuoteReserve: 1_000_000_000,
		SqrtPrice:    cfg.SqrtStartPrice,
	}
	allocations := testing.AllocsPerRun(100, func() {
		_ = params.Metrics(&pool, "")
	})
	if allocations != 0 {
		t.Errorf("pricing allocated %.0f times per run, want 0", allocations)
	}
}

func TestU128RoundTripsThroughBigInt(t *testing.T) {
	for _, want := range []U128{
		{}, {Lo: 1}, {Lo: math.MaxUint64}, {Lo: math.MaxUint64, Hi: math.MaxUint64},
		{Lo: 12345, Hi: 678},
	} {
		got, ok := bigU128(u128Big(want))
		if !ok || got != want {
			t.Errorf("round trip of %v gave %v (ok=%v)", want, got, ok)
		}
	}
	// 2^128 does not fit, and must be refused rather than wrapped.
	if _, ok := bigU128(new(big.Int).Lsh(big.NewInt(1), 128)); ok {
		t.Error("2^128 should not narrow to a U128")
	}
}

func bigMust(t *testing.T, decimal string) U128 {
	t.Helper()
	v, ok := new(big.Int).SetString(decimal, 10)
	if !ok {
		t.Fatalf("bad decimal literal %q", decimal)
	}
	out, fits := bigU128(v)
	if !fits {
		t.Fatalf("%s does not fit 128 bits", decimal)
	}
	return out
}

func BenchmarkDbcPrice(b *testing.B) {
	cfg := dbcConfig()
	params := NewMeteoraDbcParams(&cfg, 9)
	pool := MeteoraDbcVirtualPool{
		BaseReserve: cfg.SwapBaseAmount, SqrtPrice: cfg.SqrtStartPrice,
	}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = params.Metrics(&pool, "")
	}
}

func BenchmarkLaunchLabPrice(b *testing.B) {
	params := launchlabParams(CurveTypeConstantProduct)
	pool := launchlabPool()
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = params.Metrics(&pool, "")
	}
}
