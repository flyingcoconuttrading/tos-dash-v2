# src/ui/volume_tracker.py
from collections import deque
import pandas as pd


# Maximum ticks of history to retain per symbol.
# At 10s refresh: 6 = ~1 min, 30 = ~5 min, 60 = ~10 min
MAX_HISTORY = 60


class VolumeTracker:
    """
    Tracks per-symbol cumulative volume across refresh ticks and detects
    unusual activity using two independently tunable signals:

      Current rate  — short EMA over the last `ema_span` interval rates
      Baseline rate — simple SMA over the last `sma_window` interval rates

    Surge signal = (EMA_current - SMA_baseline) / SMA_baseline * 100

    This means:
      - EMA span small  (2-3)  → very reactive to latest tick
      - EMA span large  (8-10) → smoothed current signal, less noise
      - SMA window small (5)   → short memory baseline, adapts quickly
      - SMA window large (30+) → stable baseline, only big moves stand out

    Note on volume direction: TOS RTD VOLUME is total contracts traded
    (buys + sells combined). Unusual activity may reflect a large buyer,
    large seller, or both — direction cannot be inferred from this field alone.
    """

    def __init__(self):
        # { symbol: deque of (tick_index, cumulative_volume) }
        self._history: dict[str, deque] = {}
        self._tick = 0  # monotonic counter, increments on every update() call
        self._last_data: dict = {}  # most recent raw RTD data snapshot

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, data: dict, option_symbols: list):
        """Record one snapshot of volume for all option symbols."""
        self._tick += 1
        self._last_data = data
        for sym in option_symbols:
            raw = data.get(f"{sym}:VOLUME")
            try:
                vol = float(raw) if raw is not None else None
            except (ValueError, TypeError):
                vol = None

            if vol is None:
                continue

            if sym not in self._history:
                self._history[sym] = deque(maxlen=MAX_HISTORY)

            self._history[sym].append((self._tick, vol))

    def get_surge_table(
        self,
        threshold_pct: float = 50.0,
        ema_span: int = 3,
        sma_window: int = 20,
    ) -> pd.DataFrame:
        """
        Return a DataFrame of options whose EMA current rate exceeds
        the SMA baseline rate by at least threshold_pct %.

        Parameters
        ----------
        threshold_pct : float
            Minimum % above baseline to include a row.
        ema_span : int
            Number of recent interval rates to use for the EMA (current signal).
        sma_window : int
            Number of interval rates to use for the SMA (baseline).
            Should be larger than ema_span.

        Columns returned
        ----------------
        Symbol, Strike, Type, EMA Rate, SMA Baseline, Change %, Ticks
        Sorted by Change % descending.
        """
        # Need enough history for at least the baseline window + 1
        min_ticks_needed = sma_window + 1

        rows = []
        for sym, hist in self._history.items():
            if len(hist) < max(4, ema_span + 1):
                # Not enough data for even a minimal signal
                continue

            rates = self._compute_rates(hist)
            if len(rates) < 3:
                continue

            ema_rate = self._compute_ema(rates, span=ema_span)

            # SMA baseline uses up to sma_window rates, excluding the
            # most recent ema_span rates so signal and baseline don't overlap
            baseline_rates = rates[:-ema_span] if len(rates) > ema_span else rates[:-1]
            baseline_rates = baseline_rates[-sma_window:]  # cap to window

            if not baseline_rates:
                continue

            sma_baseline = sum(baseline_rates) / len(baseline_rates)

            if sma_baseline <= 0:
                continue

            change_pct = ((ema_rate - sma_baseline) / sma_baseline) * 100.0

            if change_pct < threshold_pct:
                continue

            strike, option_type = self._parse_symbol(sym)

            def _safe(key):
                try:
                    v = self._last_data.get(key)
                    return float(v) if v is not None else None
                except (ValueError, TypeError):
                    return None

            last_price = _safe(f"{sym}:LAST")
            delta      = _safe(f"{sym}:DELTA")

            rows.append({
                "Symbol":       sym,
                "Strike":       strike,
                "Type":         option_type,
                "Last":         round(last_price, 2) if last_price is not None else "—",
                "Delta":        round(delta, 3)      if delta      is not None else "—",
                "EMA Rate":     round(ema_rate, 1),
                "SMA Baseline": round(sma_baseline, 1),
                "Change %":     round(change_pct, 1),
                "Ticks":        len(hist),
            })

        if not rows:
            return pd.DataFrame(columns=[
                "Symbol", "Strike", "Type", "Last", "Delta",
                "EMA Rate", "SMA Baseline", "Change %", "Ticks"
            ])

        df = pd.DataFrame(rows)
        df = df.sort_values("Change %", ascending=False).reset_index(drop=True)
        return df

    def reset(self):
        """Clear all history — call when the user stops/restarts."""
        self._history.clear()
        self._last_data = {}
        self._tick = 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_rates(hist: deque) -> list:
        """
        Convert a deque of (tick, cumulative_volume) into per-interval rates.
        Negative deltas (e.g. EOD reset) are treated as zero.
        """
        items = list(hist)
        rates = []
        for i in range(1, len(items)):
            prev_tick, prev_vol = items[i - 1]
            curr_tick, curr_vol = items[i]
            tick_delta = max(curr_tick - prev_tick, 1)
            vol_delta = max(curr_vol - prev_vol, 0.0)
            rates.append(vol_delta / tick_delta)
        return rates

    @staticmethod
    def _compute_ema(rates: list, span: int) -> float:
        """
        Compute an EMA over the last `span` values in rates.
        Uses the standard smoothing factor: alpha = 2 / (span + 1).
        Falls back to the mean if fewer values than span.
        """
        if not rates:
            return 0.0

        window = rates[-span:] if len(rates) >= span else rates
        alpha = 2.0 / (len(window) + 1)
        ema = window[0]
        for val in window[1:]:
            ema = alpha * val + (1 - alpha) * ema
        return ema

    @staticmethod
    def _parse_symbol(sym: str) -> tuple:
        """
        Extract strike and option type from a TOS option symbol.
        e.g. '.SPY260313C675' -> ('675', 'Call')
        """
        try:
            if 'C' in sym:
                return sym.split('C')[-1], 'Call'
            elif 'P' in sym:
                return sym.split('P')[-1], 'Put'
        except Exception:
            pass
        return '?', '?'
