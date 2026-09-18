package launchpad

// Account dispatch.
//
// Classification is by (owner program, 8-byte account discriminator) and
// nothing else: no mint lists, no platform allowlists, no index. Feed every
// account update a program emits through DecodeAccount and it reports what the
// bytes are, or that they are not curve state.

// AccountKind names a decoded account type. The concrete values are generated
// alongside the layouts.
type AccountKind string

// KindUnknown means the bytes are not a known account of that program.
const KindUnknown AccountKind = ""

const discriminatorLen = 8

func discriminatorOf(data []byte) (out [8]byte, ok bool) {
	if len(data) < discriminatorLen {
		return out, false
	}
	copy(out[:], data[:discriminatorLen])
	return out, true
}

// Identify reports which account type the bytes carry, given the program that
// owns the account. Cheap enough to run on every account update: one map
// lookup, no allocation, no body decode.
//
// The program argument is not optional padding. Anchor derives discriminators
// from the struct name alone, so they are unique only within a program:
// PumpSwap's GlobalConfig and Raydium LaunchLab's GlobalConfig share the same
// eight bytes, as do PumpSwap's Pool and Vertigo's Pool. Identifying on the
// discriminator alone would decode one program's account with another's layout.
func Identify(programID string, data []byte) AccountKind {
	disc, ok := discriminatorOf(data)
	if !ok {
		return KindUnknown
	}
	if kind, found := accountRegistry[accountKey{programID, disc}]; found {
		return kind
	}
	return KindUnknown
}

func expect(programID string, data []byte, want AccountKind, label string) error {
	if Identify(programID, data) != want {
		return &DecodeError{Reason: "not a " + label + " account"}
	}
	return nil
}

// DecodePumpfunBondingCurve fills c from raw account bytes.
//
// The returned string names the first field the account was too short to hold.
// That is not an error: pump.fun appends fields to this struct and reallocs
// accounts lazily, so a live cluster holds accounts written by several
// generations of the program at once. Everything the prefix covers is valid.
func DecodePumpfunBondingCurve(data []byte, c *PumpfunBondingCurve) (truncatedAt string, err error) {
	if err := expect(PumpfunProgramID, data, KindPumpfunBondingCurve, "pump.fun BondingCurve"); err != nil {
		return "", err
	}
	return c.decodeAt(data, discriminatorLen), nil
}

// DecodePumpfunGlobal fills g from raw account bytes.
func DecodePumpfunGlobal(data []byte, g *PumpfunGlobal) (truncatedAt string, err error) {
	if err := expect(PumpfunProgramID, data, KindPumpfunGlobal, "pump.fun Global"); err != nil {
		return "", err
	}
	return g.decodeAt(data, discriminatorLen), nil
}

// DecodeRaydiumLaunchlabPoolState fills p from raw account bytes.
func DecodeRaydiumLaunchlabPoolState(data []byte, p *RaydiumLaunchlabPoolState) (string, error) {
	if err := expect(RaydiumLaunchlabProgramID, data, KindRaydiumLaunchlabPoolState, "Raydium LaunchLab PoolState"); err != nil {
		return "", err
	}
	return p.decodeAt(data, discriminatorLen), nil
}

// DecodeRaydiumLaunchlabGlobalConfig fills g from raw account bytes.
func DecodeRaydiumLaunchlabGlobalConfig(data []byte, g *RaydiumLaunchlabGlobalConfig) (string, error) {
	if err := expect(RaydiumLaunchlabProgramID, data, KindRaydiumLaunchlabGlobalConfig, "Raydium LaunchLab GlobalConfig"); err != nil {
		return "", err
	}
	return g.decodeAt(data, discriminatorLen), nil
}

// DecodeRaydiumLaunchlabPlatformConfig fills pc from raw account bytes. There
// is one of these per tenant (LetsBonk.fun, Cook.meme, ...), and it carries
// only the fee split -- a pool prices correctly without it.
func DecodeRaydiumLaunchlabPlatformConfig(data []byte, pc *RaydiumLaunchlabPlatformConfig) (string, error) {
	if err := expect(RaydiumLaunchlabProgramID, data, KindRaydiumLaunchlabPlatformConfig, "Raydium LaunchLab PlatformConfig"); err != nil {
		return "", err
	}
	return pc.decodeAt(data, discriminatorLen), nil
}

// DecodeMeteoraDbcVirtualPool fills p from raw account bytes.
func DecodeMeteoraDbcVirtualPool(data []byte, p *MeteoraDbcVirtualPool) (string, error) {
	if err := expect(MeteoraDbcProgramID, data, KindMeteoraDbcVirtualPool, "Meteora DBC VirtualPool"); err != nil {
		return "", err
	}
	return p.decodeAt(data, discriminatorLen), nil
}

// DecodeMeteoraDbcPoolConfig fills c from raw account bytes.
func DecodeMeteoraDbcPoolConfig(data []byte, c *MeteoraDbcPoolConfig) (string, error) {
	if err := expect(MeteoraDbcProgramID, data, KindMeteoraDbcPoolConfig, "Meteora DBC PoolConfig"); err != nil {
		return "", err
	}
	return c.decodeAt(data, discriminatorLen), nil
}

// Program ids for the launchpads with a Go decoder.
const (
	RaydiumLaunchlabProgramID = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
	MeteoraDbcProgramID       = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
	PumpswapProgramID         = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
)
