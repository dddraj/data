package launchpad

// pump.fun -- program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
//
// Every launch parameter is program state, not a constant: the single Global
// account (PDA of ["global"], 4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf)
// holds the initial virtual reserves, the sellable supply and the total supply.
// Read those and the launch price, market cap and raise target all fall out of
// x*y=k.
//
// The four leading u64s sit at account offsets 8/16/24/32 in every generation
// of this account, verified against all 16 published revisions of the IDL. The
// only change at those offsets was a rename in the "USDC paired coins"
// revision: virtual_sol_reserves -> virtual_quote_reserves and
// real_sol_reserves -> real_quote_reserves. Same bytes, same positions -- but
// after it "quote" appears in both the virtual and the real field names, which
// is an easy way to wire the two together wrongly.

const (
	PumpfunProgramID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
	PumpfunGlobalPDA = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"

	// pump.fun mints at 6 decimals.
	PumpfunBaseDecimals = 6
)

// PumpfunParams holds everything derived from the Global account. Build it once
// per config and reuse it for every curve: the raise target and graduation
// price need 128-bit division, and neither depends on the curve.
type PumpfunParams struct {
	InitialVirtualBase  uint64
	InitialVirtualQuote uint64
	InitialRealBase     uint64
	TokenTotalSupply    uint64
	FeeBasisPoints      uint64

	// Variants holds every opening quote reserve the program seeds curves
	// with. pump.fun uses one for SOL-quoted coins and another for non-SOL
	// ones, and the curve account does not say which it is -- the difference
	// between its virtual and real quote reserve does.
	Variants VariantCandidates

	// derived once
	RaiseTargetRaw     uint64
	LaunchPriceRaw     float64
	GraduationPriceRaw float64
	QuoteDecimals      int
	// FromChain is false when the params were not read from a Global account.
	FromChain bool
}

// NewPumpfunParams derives the reusable constants from a decoded Global.
//
// quoteDecimals is 9 for SOL-quoted coins. pump.fun also supports non-SOL
// quotes, whose opening reserve is Global.InitialVirtualQuoteReserves rather
// than InitialVirtualSolReserves and whose quote mint is typically 6 decimals;
// mixing the two up scales every price by a thousand.
func NewPumpfunParams(g *PumpfunGlobal, quoteDecimals int) PumpfunParams {
	p := PumpfunParams{
		InitialVirtualBase:  g.InitialVirtualTokenReserves,
		InitialVirtualQuote: g.InitialVirtualSolReserves,
		InitialRealBase:     g.InitialRealTokenReserves,
		TokenTotalSupply:    g.TokenTotalSupply,
		FeeBasisPoints:      g.FeeBasisPoints,
		QuoteDecimals:       quoteDecimals,
		FromChain:           true,
		Variants: VariantCandidates{
			"sol":   g.InitialVirtualSolReserves,
			"quote": g.InitialVirtualQuoteReserves,
		},
	}
	p.derive()
	return p
}

func (p *PumpfunParams) derive() {
	if p.InitialVirtualBase == 0 || p.InitialVirtualQuote == 0 {
		return
	}
	p.LaunchPriceRaw = CPPriceRaw(p.InitialVirtualQuote, p.InitialVirtualBase)
	if target, ok := CPRaiseForSupply(p.InitialVirtualQuote, p.InitialVirtualBase, p.InitialRealBase); ok {
		p.RaiseTargetRaw = target
	}
	if final, ok := CPFinalPriceRaw(p.InitialVirtualQuote, p.InitialVirtualBase, p.InitialRealBase); ok {
		p.GraduationPriceRaw = final
	}
}

// Metrics turns one decoded bonding curve into the unified answer.
//
// truncatedAt is what DecodeAccount reported; pass "" when the account decoded
// in full.
func (p PumpfunParams) Metrics(c *PumpfunBondingCurve, truncatedAt string) Metrics {
	baseDec, quoteDec := PumpfunBaseDecimals, p.QuoteDecimals
	if quoteDec == 0 {
		quoteDec = 9
	}

	// Which opening was this curve seeded with? Read it off the reserves
	// rather than assuming, or a non-SOL-quoted curve is priced against the
	// SOL opening and every number comes out wrong.
	variant, _ := ClassifyVariant(c.VirtualQuoteReserves, c.RealQuoteReserves, p.Variants)
	openingQuote := p.InitialVirtualQuote
	if variant != "" {
		openingQuote = p.Variants[variant]
	}
	curveType := "constant_product:unclassified"
	if variant != "" {
		curveType = "constant_product:" + variant
	}
	derived := p
	if openingQuote != p.InitialVirtualQuote {
		derived.InitialVirtualQuote = openingQuote
		derived.derive()
	}

	paramsSource := SourceOnchainConfig
	if !p.FromChain {
		paramsSource = SourceFallback
	}

	totalSupply := c.TokenTotalSupply
	if totalSupply == 0 {
		totalSupply = p.TokenTotalSupply
	}

	m := Metrics{
		Launchpad:     "pumpfun",
		ProgramID:     PumpfunProgramID,
		Family:        FamilyConstantProductVirtual,
		CurveType:     curveType,
		BaseMint:      Pubkey{},
		QuoteMint:     c.QuoteMint,
		BaseDecimals:  baseDec,
		QuoteDecimals: quoteDec,
		TotalSupply:   UIAmount(totalSupply, baseDec),
		TokensForSale: UIAmount(p.InitialRealBase, baseDec),
		Raised:        UIAmount(c.RealQuoteReserves, quoteDec),
		RaiseTarget:   UIAmount(derived.RaiseTargetRaw, quoteDec),
		Complete:      c.Complete,
		Migrated:      c.Complete,
		FeeBps:        float64(p.FeeBasisPoints + c.CreatorFeeBps),
		PriceSource:   SourceOnchainState,
		ParamsSource:  paramsSource,
		TruncatedAt:   truncatedAt,
	}
	m.LaunchPrice = PriceUI(derived.LaunchPriceRaw, baseDec, quoteDec)
	m.GraduationPrice = PriceUI(derived.GraduationPriceRaw, baseDec, quoteDec)
	if c.VirtualTokenReserves > 0 {
		m.CurrentPrice = PriceUI(
			CPPriceRaw(c.VirtualQuoteReserves, c.VirtualTokenReserves), baseDec, quoteDec)
	}
	if p.InitialRealBase >= c.RealTokenReserves {
		m.TokensSold = UIAmount(p.InitialRealBase-c.RealTokenReserves, baseDec)
	}

	// Does the pair actually lie on the curve? A pair that is individually
	// plausible but whose product is not k means the two reserves did not come
	// from the same read, and nothing else would catch it.
	m.Violations = CheckConstantProductPair(
		c.VirtualTokenReserves, c.VirtualQuoteReserves,
		derived.InitialVirtualBase, derived.InitialVirtualQuote)
	m.Violations = append(m.Violations,
		CheckReserveOffset(c.VirtualQuoteReserves, c.RealQuoteReserves, p.Variants)...)
	if len(m.Violations) > 0 {
		m.PriceSource = "SUSPECT"
		m.SuspectField, _ = DiagnosePair(
			c.VirtualTokenReserves, c.VirtualQuoteReserves,
			derived.InitialVirtualBase, derived.InitialVirtualQuote, derived.InitialRealBase)
	}

	m.fillDerived()
	return m
}
