export interface FxTable {
  currency_date: string;
  destination: string | null;
  rates: Record<string, string>;
  source: string | null;
  simulated: boolean;
}

export type FxPairStatus =
  | { kind: "same"; currency: string }
  | { kind: "rate"; from: string; to: string; rate: string }
  | {
      kind: "unavailable";
      from: string;
      to: string;
      recordedDestination: string | null;
    };

function clean(code: string | null | undefined): string {
  return (code ?? "").trim().toUpperCase();
}

/**
 * Select the single configured pair (from -> to) out of a recorded
 * file rate table. Recorded rates are destination units per one unit
 * of the key currency, so the displayed rate is rates[from] used
 * as-is — never inverted, never guessed. A rate only counts when
 * the table was recorded for the selected destination; anything
 * else (missing pair, changed settings, non-numeric value) is
 * unavailable rather than a silent wrong number.
 */
export function selectFxPair(
  fx: FxTable,
  from: string,
  to: string,
): FxPairStatus {
  const src = clean(from);
  const dst = clean(to);
  if (!src || !dst) {
    return {
      kind: "unavailable",
      from: src,
      to: dst,
      recordedDestination: clean(fx.destination) || null,
    };
  }
  if (src === dst) {
    return { kind: "same", currency: src };
  }
  const recorded = clean(fx.destination) || null;
  const raw = recorded === dst ? (fx.rates ?? {})[src] : undefined;
  if (typeof raw === "string" && raw !== "" && Number.isFinite(Number(raw))) {
    return { kind: "rate", from: src, to: dst, rate: raw };
  }
  return { kind: "unavailable", from: src, to: dst, recordedDestination: recorded };
}
