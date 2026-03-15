try:
    import plotly.graph_objects as go
    _PLOTLY_OK = True
except ImportError:
    go = None
    _PLOTLY_OK = False



# ---------------------------------------------------------------------------
# Shared utility
# ---------------------------------------------------------------------------

def calculate_max_pain(data: dict, strikes: list, option_symbols: list) -> float | None:
    """
    Calculate the max pain strike — the price at which the total intrinsic
    value of all expiring options (calls + puts) is minimized.

    For each candidate strike K:
        pain(K) = Σ put_OI[i]  * max(K - strike[i], 0) * 100   (calls ITM)
                + Σ call_OI[i] * max(strike[i] - K, 0) * 100   (puts ITM)

    Returns the strike with the lowest total pain, or None if data is missing.
    """
    if not strikes or not option_symbols:
        return None

    # Gather OI per strike
    call_oi_map = {}
    put_oi_map = {}

    for strike in strikes:
        try:
            call_sym = next(sym for sym in option_symbols if sym.endswith(f'C{strike}'))
            put_sym  = next(sym for sym in option_symbols if sym.endswith(f'P{strike}'))

            def safe(key):
                try:
                    v = data.get(key)
                    return float(v) if v is not None else 0.0
                except (ValueError, TypeError):
                    return 0.0

            call_oi_map[strike] = safe(f"{call_sym}:OPEN_INT")
            put_oi_map[strike]  = safe(f"{put_sym}:OPEN_INT")
        except StopIteration:
            call_oi_map[strike] = 0.0
            put_oi_map[strike]  = 0.0

    # For each candidate settlement price K, sum total pain
    min_pain = None
    max_pain_strike = None

    for K in strikes:
        pain = 0.0
        for s in strikes:
            # Calls are ITM when K > s → call holders receive max(K-s, 0)
            pain += call_oi_map[s] * max(K - s, 0) * 100
            # Puts are ITM when s > K → put holders receive max(s-K, 0)
            pain += put_oi_map[s]  * max(s - K, 0) * 100

        if min_pain is None or pain < min_pain:
            min_pain = pain
            max_pain_strike = K

    return max_pain_strike



def calculate_walls(data: dict, strikes: list, option_symbols: list) -> tuple:
    """
    Call Wall  — strike with highest call open interest. Acts as resistance.
    Put Wall   — strike with highest put open interest.  Acts as support.
    Raw OI matches Streamlit and standard market convention.
    Returns (call_wall, put_wall), either may be None.
    """
    if not strikes or not option_symbols:
        return None, None

    max_call_oi, max_put_oi = -1.0, -1.0
    call_wall, put_wall = None, None

    def safe(key):
        try:
            v = data.get(key)
            return float(v) if v is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    for strike in strikes:
        try:
            call_sym = next(sym for sym in option_symbols if sym.endswith(f'C{strike}'))
            put_sym  = next(sym for sym in option_symbols if sym.endswith(f'P{strike}'))

            c_oi = safe(f"{call_sym}:OPEN_INT")
            p_oi = safe(f"{put_sym}:OPEN_INT")

            if c_oi > max_call_oi:
                max_call_oi = c_oi
                call_wall = strike

            if p_oi > max_put_oi:
                max_put_oi = p_oi
                put_wall = strike

        except StopIteration:
            continue

    return call_wall, put_wall


# ---------------------------------------------------------------------------
# Gamma Exposure Chart
# ---------------------------------------------------------------------------

class GammaChartBuilder:
    def __init__(self, symbol: str):
        self.symbol = symbol

    def create_empty_chart(self) -> "go.Figure":
        """Create initial empty chart"""
        fig = go.Figure()
        self._set_layout(fig, 1)
        return fig

    def create_chart(self, data: dict, strikes: list, option_symbols: list) -> "go.Figure":
        """Build and return the gamma exposure chart"""
        if not _PLOTLY_OK:
            raise ImportError("plotly not installed")
        fig = go.Figure()

        current_price = float(data.get(f"{self.symbol}:LAST", 0))
        if current_price == 0:
            return self.create_empty_chart()

        pos_gex_values, neg_gex_values = self._calculate_gex_values(data, strikes, option_symbols)

        pos_values = [x for x in pos_gex_values]
        neg_values = [x for x in neg_gex_values]

        max_pos_idx = pos_values.index(max(pos_values)) if any(pos_values) else -1
        max_neg_idx = neg_values.index(min(neg_values)) if any(neg_values) else -1

        max_pos_strike = strikes[max_pos_idx] if max_pos_idx >= 0 else None
        max_neg_strike = strikes[max_neg_idx] if max_neg_idx >= 0 else None

        max_pos = max(pos_values) if pos_values else 0
        min_neg = min(neg_values) if neg_values else 0
        max_abs_value = max(abs(min_neg), abs(max_pos))

        if max_abs_value == 0:
            max_abs_value = 1

        padding = max_abs_value * 0.3
        chart_range = max_abs_value + padding

        # Max pain + walls
        max_pain = calculate_max_pain(data, strikes, option_symbols)
        call_wall, put_wall = calculate_walls(data, strikes, option_symbols)

        self._add_traces(fig, pos_values, neg_values, strikes,
                         max_pos_strike, max_pos, max_neg_strike, min_neg)

        # Current price line — blue, no inline label (shown in legend)
        fig.add_hline(y=current_price, line_color="blue", line_width=2)

        # Max pain line — orange dashed, no inline label
        if max_pain is not None:
            fig.add_hline(y=max_pain, line_color="orange", line_width=1, line_dash="dash")

        # Call wall — green dotted, no inline label
        if call_wall is not None:
            fig.add_hline(y=call_wall, line_color="#66bb6a", line_width=1, line_dash="dot")

        # Put wall — red dotted, no inline label
        if put_wall is not None:
            fig.add_hline(y=put_wall, line_color="#ef5350", line_width=1, line_dash="dot")

        # All labels in legend only — clean, no overlap on chart
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="lines",
            line=dict(color="blue", width=2),
            name=f"Price  ${current_price:.2f}",
            showlegend=True,
        ))
        if max_pain is not None and current_price:
            diff = current_price - max_pain
            direction = "↑" if diff > 0 else "↓"
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="lines",
                line=dict(color="orange", width=1, dash="dash"),
                name=f"Max Pain: ${max_pain}  ({abs(diff):.2f} {direction})",
                showlegend=True,
            ))
        if call_wall is not None:
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="lines",
                line=dict(color="#66bb6a", width=1, dash="dot"),
                name=f"Call Wall: ${call_wall}",
                showlegend=True,
            ))
        if put_wall is not None:
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="lines",
                line=dict(color="#ef5350", width=1, dash="dot"),
                name=f"Put Wall: ${put_wall}",
                showlegend=True,
            ))

        self._set_layout(fig, chart_range)

        return fig

    def _calculate_gex_values(self, data, strikes, option_symbols):
        pos_gex_values = []
        neg_gex_values = []

        try:
            underlying_price = float(data.get(f"{self.symbol}:LAST", 0))
        except (ValueError, TypeError):
            underlying_price = 0

        if underlying_price == 0:
            return [], []

        for strike in strikes:
            try:
                call_symbol = next(sym for sym in option_symbols if sym.endswith(f'C{strike}'))
                put_symbol  = next(sym for sym in option_symbols if sym.endswith(f'P{strike}'))

                try:
                    call_gamma = float(data.get(f"{call_symbol}:GAMMA", 0))
                except (ValueError, TypeError):
                    call_gamma = 0

                try:
                    put_gamma = float(data.get(f"{put_symbol}:GAMMA", 0))
                except (ValueError, TypeError):
                    put_gamma = 0

                try:
                    call_oi = float(data.get(f"{call_symbol}:OPEN_INT", 0))
                except (ValueError, TypeError):
                    call_oi = 0

                try:
                    put_oi = float(data.get(f"{put_symbol}:OPEN_INT", 0))
                except (ValueError, TypeError):
                    put_oi = 0

                # gamma exposure per 1% change in the underlying price
                gex = ((call_oi * call_gamma) - (put_oi * put_gamma)) * 100 * (underlying_price * underlying_price) * .01

            except Exception:
                print(f"Error calculating GEX strike: {strike}")
                gex = 0

            if gex > 0:
                pos_gex_values.append(gex)
                neg_gex_values.append(0)
            else:
                pos_gex_values.append(0)
                neg_gex_values.append(gex)

        return pos_gex_values, neg_gex_values

    def _add_traces(self, fig, pos_values, neg_values, strikes,
                    max_pos_strike=None, max_pos=0, max_neg_strike=None, min_neg=0):
        total_pos = sum(pos_values) / 1_000_000
        total_neg = sum(neg_values) / 1_000_000

        pos_label = (f"Positive GEX  |  Peak: {max_pos_strike}  +${max_pos/1_000_000:.0f}M  (Net: +${total_pos:.0f}M)"
                     if max_pos_strike else "Positive GEX")
        neg_label = (f"Negative GEX  |  Peak: {max_neg_strike}  -${abs(min_neg/1_000_000):.0f}M  (Net: ${total_neg:.0f}M)"
                     if max_neg_strike else "Negative GEX")

        fig.add_trace(go.Bar(
            x=pos_values, y=strikes, orientation='h',
            name=pos_label, marker_color='green'
        ))
        fig.add_trace(go.Bar(
            x=neg_values, y=strikes, orientation='h',
            name=neg_label, marker_color='red'
        ))

    def _set_layout(self, fig, chart_range):
        fig.update_layout(
            title=dict(
                text=f"<b>{self.symbol} Gamma Exposure</b>  <span style='font-size:12px; color:gray'>$ per 1% move</span>",
                x=0, xanchor="left",
                font=dict(size=15),
            ),
            xaxis_title=None,
            yaxis_title='Strike Price',
            barmode='overlay',
            showlegend=True,
            legend=dict(
                orientation="h",
                yanchor="top",
                y=-0.10,
                xanchor="left",
                x=0,
                font=dict(size=12),
            ),
            margin=dict(t=50, b=160),
            height=580,
            xaxis=dict(
                range=[-chart_range, chart_range],
                zeroline=True,
                zerolinewidth=2,
                zerolinecolor='black',
            )
        )


# ---------------------------------------------------------------------------
# Delta Exposure Chart
# ---------------------------------------------------------------------------

class DeltaChartBuilder:
    """
    Builds a Delta Exposure (DEX) chart.

    DEX per strike = (call_oi * call_delta + put_oi * put_delta) * 100 * underlying_price

    Positive DEX  → calls dominate  → dealers are long delta (bearish hedge pressure)
    Negative DEX  → puts dominate   → dealers are short delta (bullish hedge pressure)

    Also renders a secondary bar layer showing combined call + put volume.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol

    def create_empty_chart(self) -> "go.Figure":
        fig = go.Figure()
        self._set_layout(fig, 1)
        return fig

    def create_chart(self, data: dict, strikes: list, option_symbols: list) -> "go.Figure":
        fig = go.Figure()

        current_price = float(data.get(f"{self.symbol}:LAST", 0))
        if current_price == 0:
            return self.create_empty_chart()

        pos_dex, neg_dex, vol_values = self._calculate_values(data, strikes, option_symbols, current_price)

        # Axis range
        all_dex = pos_dex + neg_dex
        max_abs = max((abs(v) for v in all_dex), default=1)
        if max_abs == 0:
            max_abs = 1
        padding = max_abs * 0.3
        chart_range = max_abs + padding

        # Peak labels
        max_pos = max(pos_dex) if any(pos_dex) else 0
        min_neg = min(neg_dex) if any(neg_dex) else 0
        peak_pos_strike = strikes[pos_dex.index(max_pos)] if max_pos > 0 else None
        peak_neg_strike = strikes[neg_dex.index(min_neg)] if min_neg < 0 else None
        total_vol = sum(vol_values)

        pos_label = (f"Positive DEX  |  Peak: {peak_pos_strike}  +${max_pos/1_000_000:.0f}M"
                     if peak_pos_strike else "Positive DEX")
        neg_label = (f"Negative DEX  |  Peak: {peak_neg_strike}  -${abs(min_neg/1_000_000):.0f}M"
                     if peak_neg_strike else "Negative DEX")
        vol_label = f"Options Volume  |  Total: {total_vol:,.0f}"

        # Max pain + walls
        max_pain = calculate_max_pain(data, strikes, option_symbols)
        call_wall, put_wall = calculate_walls(data, strikes, option_symbols)

        # DEX bars
        fig.add_trace(go.Bar(
            x=pos_dex, y=strikes, orientation='h',
            name=pos_label, marker_color='rgba(0, 150, 0, 0.85)', xaxis='x'
        ))
        fig.add_trace(go.Bar(
            x=neg_dex, y=strikes, orientation='h',
            name=neg_label, marker_color='rgba(200, 0, 0, 0.85)', xaxis='x'
        ))

        # Volume bars (scaled to ~60% of DEX range)
        max_vol = max(vol_values) if vol_values else 1
        if max_vol == 0:
            max_vol = 1
        vol_scale = (chart_range * 0.6) / max_vol
        scaled_vol = [v * vol_scale for v in vol_values]

        fig.add_trace(go.Bar(
            x=scaled_vol, y=strikes, orientation='h',
            name=vol_label,
            marker_color='rgba(100, 100, 255, 0.3)',
            xaxis='x',
            hovertemplate='Volume: %{customdata:,.0f}<extra></extra>',
            customdata=vol_values,
        ))

        # Current price line — blue, no inline label
        fig.add_hline(y=current_price, line_color="blue", line_width=2)

        # Max pain line — orange dashed, no inline label
        if max_pain is not None:
            fig.add_hline(y=max_pain, line_color="orange", line_width=1, line_dash="dash")

        # Call wall — green dotted, no inline label
        if call_wall is not None:
            fig.add_hline(y=call_wall, line_color="#66bb6a", line_width=1, line_dash="dot")

        # Put wall — red dotted, no inline label
        if put_wall is not None:
            fig.add_hline(y=put_wall, line_color="#ef5350", line_width=1, line_dash="dot")

        # All labels in legend only
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="lines",
            line=dict(color="blue", width=2),
            name=f"Price  ${current_price:.2f}",
            showlegend=True,
        ))
        if max_pain is not None and current_price:
            diff = current_price - max_pain
            direction = "↑" if diff > 0 else "↓"
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="lines",
                line=dict(color="orange", width=1, dash="dash"),
                name=f"Max Pain: ${max_pain}  ({abs(diff):.2f} {direction})",
                showlegend=True,
            ))
        if call_wall is not None:
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="lines",
                line=dict(color="#66bb6a", width=1, dash="dot"),
                name=f"Call Wall: ${call_wall}",
                showlegend=True,
            ))
        if put_wall is not None:
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="lines",
                line=dict(color="#ef5350", width=1, dash="dot"),
                name=f"Put Wall: ${put_wall}",
                showlegend=True,
            ))

        self._set_layout(fig, chart_range)
        return fig

    def _calculate_values(self, data, strikes, option_symbols, underlying_price):
        pos_dex = []
        neg_dex = []
        vol_values = []

        for strike in strikes:
            try:
                call_sym = next(sym for sym in option_symbols if sym.endswith(f'C{strike}'))
                put_sym  = next(sym for sym in option_symbols if sym.endswith(f'P{strike}'))

                def safe_float(key, default=0.0):
                    try:
                        return float(data.get(key, default) or default)
                    except (ValueError, TypeError):
                        return default

                call_delta  = safe_float(f"{call_sym}:DELTA")
                put_delta   = safe_float(f"{put_sym}:DELTA")
                call_oi     = safe_float(f"{call_sym}:OPEN_INT")
                put_oi      = safe_float(f"{put_sym}:OPEN_INT")
                call_volume = safe_float(f"{call_sym}:VOLUME")
                put_volume  = safe_float(f"{put_sym}:VOLUME")

                dex = (call_oi * call_delta + put_oi * put_delta) * 100 * underlying_price
                total_vol = call_volume + put_volume

            except Exception:
                print(f"Error calculating DEX for strike {strike}")
                dex = 0.0
                total_vol = 0.0

            if dex >= 0:
                pos_dex.append(dex)
                neg_dex.append(0.0)
            else:
                pos_dex.append(0.0)
                neg_dex.append(dex)

            vol_values.append(total_vol)

        return pos_dex, neg_dex, vol_values

    def _set_layout(self, fig, chart_range):
        fig.update_layout(
            title=dict(
                text=f"<b>{self.symbol} Delta Exposure</b>",
                x=0, xanchor="left",
                font=dict(size=15),
            ),
            xaxis_title=None,
            yaxis_title='Strike Price',
            barmode='overlay',
            showlegend=True,
            legend=dict(
                orientation="h",
                yanchor="top",
                y=-0.10,
                xanchor="left",
                x=0,
                font=dict(size=12),
            ),
            margin=dict(t=50, b=160),
            height=580,
            xaxis=dict(
                range=[-chart_range, chart_range],
                zeroline=True,
                zerolinewidth=2,
                zerolinecolor='black',
            )
        )
