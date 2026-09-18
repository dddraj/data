package launchpad

import (
	"math"
	"testing"
)

const (
	ivt = 1_073_000_000_000_000
	ivs = 30_000_000_000
	irt = 793_100_000_000_000
)

func TestPubkeyRoundTrip(t *testing.T) {
	for _, want := range []string{
		"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
		"So11111111111111111111111111111111111111112",
		"11111111111111111111111111111111",
		"4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",
	} {
		key, err := ParsePubkey(want)
		if err != nil {
			t.Fatalf("%s: %v", want, err)
		}
		if got := key.String(); got != want {
			t.Errorf("round trip: got %s, want %s", got, want)
		}
	}
}

func TestPubkeyRejectsJunk(t *testing.T) {
	if _, err := ParsePubkey("not-valid-base58-0OIl"); err == nil {
		t.Error("expected an error for invalid characters")
	}
	if _, err := ParsePubkey("abc"); err == nil {
		t.Error("expected an error for a short address")
	}
}

func TestZeroPubkeyIsRecognised(t *testing.T) {
	key, _ := ParsePubkey("11111111111111111111111111111111")
	if !key.IsZero() {
		t.Error("the system address should report as zero")
	}
}

func TestMulU64ExceedsUint64(t *testing.T) {
	// k for pump.fun is ~3.2e25; a uint64 stops near 1.8e19, so this has to be
	// 128-bit or the invariant check silently wraps.
	k := MulU64(ivt, ivs)
	if k.Hi == 0 {
		t.Fatal("expected the product to overflow into the high word")
	}
	if got, want := k.Float(), 3.219e25; math.Abs(got-want)/want > 1e-12 {
		t.Errorf("k = %.6e, want %.6e", got, want)
	}
}

// --------------------------------------------------------------------------
// account dispatch
// --------------------------------------------------------------------------

func TestIdentifyNeedsTheOwningProgram(t *testing.T) {
	// Anchor derives discriminators from the struct name alone, so PumpSwap's
	// GlobalConfig and LaunchLab's GlobalConfig are byte-identical. Resolving
	// on the discriminator alone would decode one with the other's layout.
	if DiscPumpswapGlobalConfig != DiscRaydiumLaunchlabGlobalConfig {
		t.Skip("the two GlobalConfig discriminators no longer collide")
	}
	data := make([]byte, 200)
	copy(data, DiscRaydiumLaunchlabGlobalConfig[:])

	if got := Identify(RaydiumLaunchlabProgramID, data); got != KindRaydiumLaunchlabGlobalConfig {
		t.Errorf("LaunchLab: got %q", got)
	}
	if got := Identify(PumpswapProgramID, data); got != KindPumpswapGlobalConfig {
		t.Errorf("PumpSwap: got %q", got)
	}
	if got := Identify(PumpfunProgramID, data); got != KindUnknown {
		t.Errorf("a program that owns no such account should report unknown, got %q", got)
	}
}

func TestIdentifyRejectsShortAndUnknownData(t *testing.T) {
	if got := Identify(PumpfunProgramID, []byte{1, 2, 3}); got != KindUnknown {
		t.Errorf("short buffer: got %q", got)
	}
	if got := Identify(PumpfunProgramID, make([]byte, 64)); got != KindUnknown {
		t.Errorf("zero discriminator: got %q", got)
	}
}

func TestDecodeRejectsAnAccountOfAnotherType(t *testing.T) {
	data := make([]byte, 200)
	copy(data, DiscPumpfunGlobal[:])
	var curve PumpfunBondingCurve
	if _, err := DecodePumpfunBondingCurve(data, &curve); err == nil {
		t.Fatal("a Global account must not decode as a BondingCurve")
	}
}

// --------------------------------------------------------------------------
// curve maths
// --------------------------------------------------------------------------

func TestPumpfunRaiseTargetIsTheFamous85SOL(t *testing.T) {
	target, ok := CPRaiseForSupply(ivs, ivt, irt)
	if !ok {
		t.Fatal("expected a target")
	}
	if target != 85_005_359_057 {
		t.Errorf("raise target = %d lamports, want 85005359057", target)
	}
}

func TestPumpfunOpeningAndGraduationPrices(t *testing.T) {
	launch := PriceUI(CPPriceRaw(ivs, ivt), 6, 9)
	if math.Abs(launch-2.7958993476234855e-08)/2.7958993476234855e-08 > 1e-12 {
		t.Errorf("launch price = %.17g", launch)
	}
	if mcap := launch * 1e9; math.Abs(mcap-27.959) > 0.001 {
		t.Errorf("launch market cap = %.4f SOL, want ~27.959", mcap)
	}
	final, ok := CPFinalPriceRaw(ivs, ivt, irt)
	if !ok {
		t.Fatal("expected a graduation price")
	}
	if mcap := PriceUI(final, 6, 9) * 1e9; math.Abs(mcap-410.88) > 0.01 {
		t.Errorf("graduation market cap = %.2f SOL, want ~410.88", mcap)
	}
}

func TestQuoteInRoundsUpSoTheBuyerAlwaysGetsEnough(t *testing.T) {
	const wanted = 10_000_000_000_000
	quote, ok := CPQuoteIn(ivs, ivt, wanted)
	if !ok {
		t.Fatal("expected a quote")
	}
	if got := CPBaseOut(ivs, ivt, quote); got < wanted {
		t.Errorf("rounding left the buyer short: %d < %d", got, wanted)
	}
	if got := CPBaseOut(ivs, ivt, quote-1); got >= wanted {
		t.Errorf("over-charged: %d still reaches %d", quote-1, wanted)
	}
}

func TestBuyingTheWholeVirtualReserveIsRejected(t *testing.T) {
	if _, ok := CPQuoteIn(ivs, ivt, ivt); ok {
		t.Error("draining the entire virtual base must not be quotable")
	}
	if _, ok := CPRaiseForSupply(ivs, ivt, 0); ok {
		t.Error("a zero sellable supply has no raise target")
	}
}

func TestPriceUIAppliesTheDecimalDifference(t *testing.T) {
	// A 6-decimal token quoted in 9-decimal SOL is a factor of 1e-3; getting
	// this wrong is the classic thousand-fold error.
	if got := PriceUI(1, 6, 9); math.Abs(got-1e-3) > 1e-15 {
		t.Errorf("PriceUI(1, 6, 9) = %g, want 1e-3", got)
	}
	if got := PriceUI(1, 6, 6); got != 1 {
		t.Errorf("PriceUI(1, 6, 6) = %g, want 1", got)
	}
}

func TestSqrtPriceSquaresToThePrice(t *testing.T) {
	// 0.25 as a Q64.64 sqrt price is 0.5 * 2^64.
	sqrtPrice := U128{Hi: 0, Lo: 1 << 63}
	if got := SqrtPriceToRawPrice(sqrtPrice); math.Abs(got-0.25) > 1e-12 {
		t.Errorf("got %g, want 0.25", got)
	}
	if got := SqrtPriceToRawPrice(U128{}); got != 0 {
		t.Errorf("a zero sqrt price should give zero, got %g", got)
	}
}

func TestProgressIsClamped(t *testing.T) {
	if got := Progress(50, 100); got != 0.5 {
		t.Errorf("got %g", got)
	}
	if got := Progress(500, 100); got != 1 {
		t.Errorf("got %g, want clamped to 1", got)
	}
	if got := Progress(-5, 100); got != 0 {
		t.Errorf("got %g, want clamped to 0", got)
	}
	if got := Progress(5, 0); got != 0 {
		t.Errorf("a zero target should give zero, got %g", got)
	}
}

// --------------------------------------------------------------------------
// invariants
// --------------------------------------------------------------------------

func TestHealthyPairsPassTheInvariant(t *testing.T) {
	if v := CheckConstantProductPair(ivt, ivs, ivt, ivs); v != nil {
		t.Errorf("an untouched curve should be clean, got %v", v)
	}
	const paid = 42_500_000_000
	vQuote := uint64(ivs + paid)
	// k is ~3.2e25, so this division has to go through 128 bits.
	vBase := divU128ByU64(MulU64(ivt, ivs), vQuote)
	if v := CheckConstantProductPair(vBase, vQuote, ivt, ivs); v != nil {
		t.Errorf("a mid-curve pair should be clean, got %v", v)
	}
}

func TestFlooringDustDoesNotTripTheCheck(t *testing.T) {
	vQuote := uint64(ivs + 1_000_000_000)
	vBase := divU128ByU64(MulU64(ivt, ivs), vQuote) + 5_000
	if v := CheckConstantProductPair(vBase, vQuote, ivt, ivs); v != nil {
		t.Errorf("rounding dust must not be an error, got %v", v)
	}
}

func TestTheMeasuredProductionAnomalyIsCaught(t *testing.T) {
	const anomalyBase = 1_077_887_039_606_396
	const anomalyQuote = 18_204_928_211

	violations := CheckConstantProductPair(anomalyBase, anomalyQuote, ivt, ivs)
	seen := map[string]bool{}
	for _, v := range violations {
		seen[v.Name] = true
	}
	for _, want := range []string{"k_violation", "quote_below_open", "base_above_open"} {
		if !seen[want] {
			t.Errorf("missing violation %q; got %v", want, violations)
		}
	}

	field, explanation := DiagnosePair(anomalyBase, anomalyQuote, ivt, ivs, irt)
	if field != "virtual_quote" {
		t.Errorf("suspect field = %q, want virtual_quote", field)
	}
	if explanation == "" {
		t.Error("expected an explanation naming the implied value")
	}
}

func TestDiagnoseNamesTheBaseWhenThatIsTheOddOne(t *testing.T) {
	field, _ := DiagnosePair(uint64(ivt)*2, uint64(ivs)+10_000_000_000, ivt, ivs, irt)
	if field != "virtual_base" {
		t.Errorf("suspect field = %q, want virtual_base", field)
	}
}

func TestDiagnoseIsSilentOnAConsistentPair(t *testing.T) {
	field, explanation := DiagnosePair(ivt, ivs, ivt, ivs, irt)
	if field != "" {
		t.Errorf("a consistent pair should have no suspect, got %q", field)
	}
	if explanation != "pair is consistent with the opening k" {
		t.Errorf("unexpected explanation %q", explanation)
	}
}

func TestZeroesAreNotReportedAsViolations(t *testing.T) {
	if v := CheckConstantProductPair(0, ivs, ivt, ivs); v != nil {
		t.Errorf("missing data is not a violation, got %v", v)
	}
	if field, _ := DiagnosePair(0, 0, ivt, ivs, irt); field != "" {
		t.Errorf("missing data should have no suspect, got %q", field)
	}
}

// --------------------------------------------------------------------------
// allocation budget
// --------------------------------------------------------------------------

func TestDecodingACurveDoesNotAllocate(t *testing.T) {
	data := make([]byte, 8+125)
	copy(data, DiscPumpfunBondingCurve[:])
	var curve PumpfunBondingCurve
	allocations := testing.AllocsPerRun(100, func() {
		_, _ = DecodePumpfunBondingCurve(data, &curve)
	})
	if allocations != 0 {
		t.Errorf("decode allocated %.0f times per run, want 0", allocations)
	}
}

func TestPricingAHealthyCurveDoesNotAllocate(t *testing.T) {
	params := PumpfunParams{
		InitialVirtualBase:  ivt,
		InitialVirtualQuote: ivs,
		InitialRealBase:     irt,
		TokenTotalSupply:    1_000_000_000_000_000,
		QuoteDecimals:       9,
		FromChain:           true,
	}
	params.derive()

	curve := PumpfunBondingCurve{
		VirtualTokenReserves: ivt,
		VirtualQuoteReserves: ivs,
		RealTokenReserves:    irt,
		TokenTotalSupply:     1_000_000_000_000_000,
	}
	allocations := testing.AllocsPerRun(100, func() {
		_ = params.Metrics(&curve, "")
	})
	if allocations != 0 {
		t.Errorf("pricing allocated %.0f times per run, want 0", allocations)
	}
}

func BenchmarkDecodeAndPrice(b *testing.B) {
	data := make([]byte, 8+125)
	copy(data, DiscPumpfunBondingCurve[:])
	params := PumpfunParams{
		InitialVirtualBase: ivt, InitialVirtualQuote: ivs,
		InitialRealBase: irt, QuoteDecimals: 9, FromChain: true,
	}
	params.derive()

	b.ReportAllocs()
	b.ResetTimer()
	var curve PumpfunBondingCurve
	for i := 0; i < b.N; i++ {
		truncatedAt, _ := DecodePumpfunBondingCurve(data, &curve)
		_ = params.Metrics(&curve, truncatedAt)
	}
}

// --------------------------------------------------------------------------
// variant classification
// --------------------------------------------------------------------------

const ivq = 4_292_000_000 // pump.fun's opening for non-SOL quote pairs

func variants() VariantCandidates {
	return VariantCandidates{"sol": ivs, "quote": ivq}
}

func TestReserveOffsetClassifiesATradedSolCurve(t *testing.T) {
	// The offset is fixed for the curve's life, so trading does not blur it --
	// which is exactly where counting curves near their opening value fails.
	for _, paid := range []uint64{0, 1_000_000_000, 42_500_000_000, 85_005_359_057} {
		variant, _ := ClassifyVariant(ivs+paid, paid, variants())
		if variant != "sol" {
			t.Errorf("paid %d: got %q, want sol", paid, variant)
		}
	}
}

func TestReserveOffsetClassifiesATradedQuoteCurve(t *testing.T) {
	for _, paid := range []uint64{0, 500_000_000, 12_000_000_000} {
		variant, _ := ClassifyVariant(ivq+paid, paid, variants())
		if variant != "quote" {
			t.Errorf("paid %d: got %q, want quote", paid, variant)
		}
	}
}

func TestReserveOffsetReportsAnUnclassifiablePair(t *testing.T) {
	variant, offset := ClassifyVariant(18_204_928_211, 1, variants())
	if variant != "" {
		t.Errorf("got %q, want no variant", variant)
	}
	if offset != 18_204_928_210 {
		t.Errorf("offset = %d", offset)
	}
}

func TestAVirtualColumnHoldingTheRealReserveIsNamedAsSuch(t *testing.T) {
	violations := CheckReserveOffset(670_000_000, 670_000_000, variants())
	if len(violations) != 1 || violations[0].Name != "reserve_offset_mismatch" {
		t.Fatalf("got %v", violations)
	}
	if !contains(violations[0].Detail, "carrying the REAL reserve") {
		t.Errorf("detail should name the likely cause: %s", violations[0].Detail)
	}
}

func TestHealthyCurvesRaiseNoOffsetViolation(t *testing.T) {
	if v := CheckReserveOffset(ivs+5_000_000_000, 5_000_000_000, variants()); v != nil {
		t.Errorf("SOL variant: got %v", v)
	}
	if v := CheckReserveOffset(ivq+5_000_000_000, 5_000_000_000, variants()); v != nil {
		t.Errorf("quote variant: got %v", v)
	}
	if v := CheckReserveOffset(ivs, 0, nil); v != nil {
		t.Errorf("no candidates means no judgement, got %v", v)
	}
}

func TestQuoteVariantIsPricedAgainstItsOwnOpening(t *testing.T) {
	// Judging this curve against the 30 SOL opening is what made a whole
	// population look broken when it was healthy all along.
	params := PumpfunParams{
		InitialVirtualBase: ivt, InitialVirtualQuote: ivs,
		InitialRealBase: irt, QuoteDecimals: 9, FromChain: true,
		Variants: variants(),
	}
	params.derive()

	curve := PumpfunBondingCurve{
		VirtualTokenReserves: ivt,
		VirtualQuoteReserves: ivq,
		RealQuoteReserves:    0,
		RealTokenReserves:    irt,
		TokenTotalSupply:     1_000_000_000_000_000,
	}
	m := params.Metrics(&curve, "")
	if m.CurveType != "constant_product:quote" {
		t.Errorf("curve type = %q", m.CurveType)
	}
	if !m.OK() {
		t.Errorf("a healthy quote-variant curve should not be flagged: %v", m.Violations)
	}
	if want := PriceUI(float64(ivq)/float64(ivt), 6, 9); math.Abs(m.LaunchPrice-want)/want > 1e-12 {
		t.Errorf("launch price = %g, want %g", m.LaunchPrice, want)
	}
}

func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
