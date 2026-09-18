package launchpad

// Source records where a value came from. A number read from the program's own
// config is worth more than one carried in a build, and a caller should be able
// to tell them apart without reading the code.
type Source string

const (
	// SourceOnchainState came from the curve/pool account itself.
	SourceOnchainState Source = "onchain_state"
	// SourceOnchainConfig came from the program's global/platform config.
	SourceOnchainConfig Source = "onchain_config"
	// SourceDerived was computed from the above.
	SourceDerived Source = "derived"
	// SourceFallback came from a compiled-in constant rather than the chain.
	SourceFallback Source = "fallback"
	// SourceUnavailable could not be determined.
	SourceUnavailable Source = "unavailable"
)

// CurveFamily names the shape of the curve.
type CurveFamily string

const (
	FamilyConstantProductVirtual CurveFamily = "constant_product_virtual"
	FamilyLinear                 CurveFamily = "linear"
	FamilyFixedPrice             CurveFamily = "fixed_price"
	FamilySqrtPiecewise          CurveFamily = "sqrt_piecewise"
	FamilyUnknown                CurveFamily = "unknown"
)

// Metrics is the unified answer for one launch, whatever program produced it.
//
// Prices are whole quote per whole base token; supplies are whole tokens;
// market caps are price x total supply, which is the DexScreener/Birdeye
// convention. Note that Moonit's own "market cap" is price x tokens *sold* --
// see docs/CURVE_MATH.md before comparing the two.
type Metrics struct {
	Launchpad string
	ProgramID string
	Family    CurveFamily
	CurveType string

	BaseMint      Pubkey
	QuoteMint     Pubkey
	BaseDecimals  int
	QuoteDecimals int

	TotalSupply   float64
	TokensForSale float64
	TokensSold    float64

	LaunchPrice     float64
	CurrentPrice    float64
	GraduationPrice float64
	LaunchMcap      float64
	CurrentMcap     float64
	GraduationMcap  float64

	RaiseTarget float64
	Raised      float64
	Progress    float64

	Complete bool
	Migrated bool
	FeeBps   float64

	// ParamsSource says where the launch parameters came from. SourceFallback
	// means they were not read from the program's config, so they may be stale.
	ParamsSource Source
	// PriceSource says where CurrentPrice came from; SUSPECT when the reserve
	// pair failed its invariant, in which case Violations explains why.
	PriceSource Source
	// Violations is nil for the overwhelming majority of curves.
	Violations []Violation
	// SuspectField names the reserve to distrust when a pair is inconsistent.
	SuspectField string
	// TruncatedAt names the first field the account was too short to hold,
	// which is normal for accounts written by an older build of the program.
	TruncatedAt string
}

// OK reports whether the metrics carry no blocking invariant violation. A false
// result means CurrentPrice should not be published as-is.
func (m *Metrics) OK() bool {
	for _, v := range m.Violations {
		if v.Severity == SeverityError {
			return false
		}
	}
	return true
}

// fillDerived computes market caps and progress once prices and supply are set.
func (m *Metrics) fillDerived() {
	if m.TotalSupply > 0 {
		if m.LaunchMcap == 0 {
			m.LaunchMcap = m.LaunchPrice * m.TotalSupply
		}
		if m.CurrentMcap == 0 {
			m.CurrentMcap = m.CurrentPrice * m.TotalSupply
		}
		if m.GraduationMcap == 0 {
			m.GraduationMcap = m.GraduationPrice * m.TotalSupply
		}
	}
	if m.Progress == 0 {
		m.Progress = Progress(m.Raised, m.RaiseTarget)
	}
}
