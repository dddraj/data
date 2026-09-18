package launchpad

// Raydium LaunchLab -- program LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj
//
// Multi-tenant, and unlike pump.fun almost everything is on the pool itself:
// supply, the sellable amount, the raise target, and the reserve pair. Only two
// things come from elsewhere -- which curve maths applies (GlobalConfig) and
// the platform's fee split (PlatformConfig).
//
// The three curve types differ in what virtual_base and virtual_quote mean:
//
//	constant product : virtual liquidity; price = (vQuote+realQuote)/(vBase-realBase)
//	fixed price      : price = virtual_quote / virtual_base, flat for the whole curve
//	linear           : virtual_base is the slope in Q64; price = a*sold/2^64
//
// Note the virtual reserves are written once at pool creation and never move --
// only the real ones accumulate. That is what makes the k check below work, and
// it also means a virtual reserve that differs between two pools sharing one
// config is a pipeline artefact rather than chain state.

const (
	// CurveTypeConstantProduct and friends are GlobalConfig.CurveType.
	CurveTypeConstantProduct uint8 = 0
	CurveTypeFixedPrice      uint8 = 1
	CurveTypeLinear          uint8 = 2

	// LaunchLab fee rates are in hundredths of a basis point (1e-6).
	launchLabFeeDenominator = 1_000_000

	// PoolStatus values.
	poolStatusFunding  uint8 = 0
	poolStatusMigrate  uint8 = 1
	poolStatusMigrated uint8 = 2
)

// RaydiumLaunchlabParams carries the per-program and per-platform inputs that
// a pool alone does not supply.
type RaydiumLaunchlabParams struct {
	CurveType uint8
	// TradeFeeRate, PlatformFeeRate and CreatorFeeRate are in 1e-6 units.
	TradeFeeRate    uint64
	PlatformFeeRate uint64
	CreatorFeeRate  uint64
	// MigrateFee is the config's default, used only when the pool's own
	// migrate_fee is zero.
	MigrateFee uint64
	// FromChain is false when the config was not read from a config account,
	// in which case CurveType is an assumption rather than a fact.
	FromChain bool
}

// NewRaydiumLaunchlabParams reads what is needed from the GlobalConfig. Pass
// the PlatformConfig separately via WithPlatform when it is available; without
// it the fee is understated but every price is still correct.
func NewRaydiumLaunchlabParams(g *RaydiumLaunchlabGlobalConfig) RaydiumLaunchlabParams {
	return RaydiumLaunchlabParams{
		CurveType:    g.CurveType,
		TradeFeeRate: g.TradeFeeRate,
		MigrateFee:   g.MigrateFee,
		FromChain:    true,
	}
}

// WithPlatform folds a platform's fee split into the params.
func (p RaydiumLaunchlabParams) WithPlatform(pc *RaydiumLaunchlabPlatformConfig) RaydiumLaunchlabParams {
	p.PlatformFeeRate = pc.FeeRate
	p.CreatorFeeRate = pc.CreatorFeeRate
	return p
}

// CurveTypeName renders the curve type for reporting.
func (p RaydiumLaunchlabParams) CurveTypeName() string {
	switch p.CurveType {
	case CurveTypeConstantProduct:
		return "constant_product"
	case CurveTypeFixedPrice:
		return "fixed_price"
	case CurveTypeLinear:
		return "linear"
	}
	return "unknown"
}

// Family maps the curve type onto the shared vocabulary.
func (p RaydiumLaunchlabParams) Family() CurveFamily {
	switch p.CurveType {
	case CurveTypeConstantProduct:
		return FamilyConstantProductVirtual
	case CurveTypeFixedPrice:
		return FamilyFixedPrice
	case CurveTypeLinear:
		return FamilyLinear
	}
	return FamilyUnknown
}

// Metrics turns one decoded pool into the unified answer.
func (p RaydiumLaunchlabParams) Metrics(pool *RaydiumLaunchlabPoolState, truncatedAt string) Metrics {
	baseDec := int(pool.BaseDecimals)
	quoteDec := int(pool.QuoteDecimals)

	paramsSource := SourceOnchainConfig
	if !p.FromChain {
		paramsSource = SourceFallback
	}

	m := Metrics{
		Launchpad:     "raydium_launchlab",
		ProgramID:     RaydiumLaunchlabProgramID,
		Family:        p.Family(),
		CurveType:     p.CurveTypeName(),
		BaseMint:      pool.BaseMint,
		QuoteMint:     pool.QuoteMint,
		BaseDecimals:  baseDec,
		QuoteDecimals: quoteDec,
		TotalSupply:   UIAmount(pool.Supply, baseDec),
		TokensForSale: UIAmount(pool.TotalBaseSell, baseDec),
		TokensSold:    UIAmount(pool.RealBase, baseDec),
		RaiseTarget:   UIAmount(pool.TotalQuoteFundRaising, quoteDec),
		Raised:        UIAmount(pool.RealQuote, quoteDec),
		Complete:      pool.Status >= poolStatusMigrate,
		Migrated:      pool.Status == poolStatusMigrated,
		PriceSource:   SourceOnchainState,
		ParamsSource:  paramsSource,
		TruncatedAt:   truncatedAt,
	}

	switch p.CurveType {
	case CurveTypeConstantProduct:
		m.LaunchPrice = PriceUI(CPPriceRaw(pool.VirtualQuote, pool.VirtualBase), baseDec, quoteDec)
		if pool.VirtualBase > pool.RealBase {
			m.CurrentPrice = PriceUI(
				CPPriceRaw(pool.VirtualQuote+pool.RealQuote, pool.VirtualBase-pool.RealBase),
				baseDec, quoteDec)
		}
		m.Violations = CheckLaunchLabConstantProduct(
			pool.VirtualBase, pool.VirtualQuote, pool.RealBase, pool.RealQuote)
	case CurveTypeFixedPrice:
		m.LaunchPrice = PriceUI(CPPriceRaw(pool.VirtualQuote, pool.VirtualBase), baseDec, quoteDec)
		m.CurrentPrice = m.LaunchPrice
	case CurveTypeLinear:
		// A linear curve opens at price zero and rises with tokens sold.
		m.LaunchPrice = 0
		m.CurrentPrice = PriceUI(
			LaunchLabLinearPriceRaw(pool.VirtualBase, pool.RealBase), baseDec, quoteDec)
	}

	// Graduation price is what the migrated AMM pool opens at: the raise, less
	// the migration fee, spread over the tokens held back for migration.
	migrateFee := pool.MigrateFee
	if migrateFee == 0 {
		migrateFee = p.MigrateFee
	}
	migrateTokens := int64(pool.Supply) - int64(pool.TotalBaseSell) -
		int64(pool.VestingSchedule.TotalLockedAmount)
	if migrateTokens > 0 && pool.TotalQuoteFundRaising > migrateFee {
		raw := float64(pool.TotalQuoteFundRaising-migrateFee) / float64(migrateTokens)
		m.GraduationPrice = PriceUI(raw, baseDec, quoteDec)
	}

	rates := p.TradeFeeRate + p.PlatformFeeRate + p.CreatorFeeRate
	m.FeeBps = float64(rates) / launchLabFeeDenominator * 10_000

	if len(m.Violations) > 0 {
		m.PriceSource = "SUSPECT"
	}
	m.fillDerived()
	return m
}

// CheckLaunchLabConstantProduct tests a LaunchLab pool's reserves against the k
// its virtual pair fixes.
//
// LaunchLab writes virtual_base and virtual_quote once at pool creation and
// never touches them again; only the real reserves accumulate. So the effective
// product
//
//	(virtual_base - real_base) * (virtual_quote + real_quote)
//
// must stay at virtual_base * virtual_quote, up to the same flooring dust that
// only ever moves it up. A pool that misses it has real and virtual reserves
// that did not come from the same read.
func CheckLaunchLabConstantProduct(virtualBase, virtualQuote, realBase, realQuote uint64) []Violation {
	if virtualBase == 0 || virtualQuote == 0 {
		return nil
	}
	if realBase >= virtualBase {
		return []Violation{{
			Name: "real_base_exceeds_virtual",
			Detail: "real_base is not below virtual_base, which would empty the curve; " +
				"the two did not come from one read",
			Severity: SeverityError,
		}}
	}
	return CheckConstantProductPair(
		virtualBase-realBase, virtualQuote+realQuote, virtualBase, virtualQuote)
}
