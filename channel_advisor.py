"""
channel_advisor.py — Intraday price channel detection for tos-dash-v2.

Uses linear regression on rolling 1-minute candle closes to detect channel
direction, boundaries, and price position. Also tracks volume trend.

Called every tick from api.py. Writes channel_state dict to snapshot.
"""

from collections import deque
from dataclasses import dataclass
from typing import Optional

CHANNEL_WINDOW_MIN   = 20
CHANNEL_MIN_CANDLES  = 6
VOL_WINDOW           = 10
SLOPE_FLAT_THRESHOLD = 0.02

CHANNEL_UP   = "ascending"
CHANNEL_DOWN = "descending"
CHANNEL_FLAT = "flat"
CHANNEL_NONE = "none"

POS_NEAR_TOP    = "near_top"
POS_NEAR_BOTTOM = "near_bottom"
POS_MIDDLE      = "middle"
POS_ABOVE       = "above"
POS_BELOW       = "below"


@dataclass
class ChannelState:
    direction:      str            = CHANNEL_NONE
    upper_bound:    Optional[float] = None
    lower_bound:    Optional[float] = None
    mid_bound:      Optional[float] = None
    channel_width:  Optional[float] = None
    price_position: str            = "none"
    position_pct:   Optional[float] = None
    slope:          Optional[float] = None
    candle_count:   int            = 0
    vol_trend:      str            = "neutral"
    vol_ratio_avg:  Optional[float] = None
    vol_exhaustion: bool           = False
    confidence:     str            = "low"
    valid:          bool           = False


class ChannelAdvisor:
    def __init__(self):
        self._candle_closes: deque = deque(maxlen=CHANNEL_WINDOW_MIN)
        self._candle_highs:  deque = deque(maxlen=CHANNEL_WINDOW_MIN)
        self._candle_lows:   deque = deque(maxlen=CHANNEL_WINDOW_MIN)
        self._cur_minute:    int   = -1
        self._cur_high:      float = 0.0
        self._cur_low:       float = float('inf')
        self._cur_close:     float = 0.0
        self._vol_ratio_history: deque = deque(maxlen=VOL_WINDOW)
        self._last_state:    ChannelState = ChannelState()

    def reset(self):
        self._candle_closes.clear()
        self._candle_highs.clear()
        self._candle_lows.clear()
        self._cur_minute = -1
        self._cur_high   = 0.0
        self._cur_low    = float('inf')
        self._cur_close  = 0.0
        self._vol_ratio_history.clear()
        self._last_state = ChannelState()

    def _linear_regression(self, values: list) -> tuple:
        n = len(values)
        if n < 2:
            return 0.0, values[0] if values else 0.0
        x_mean = (n - 1) / 2.0
        y_mean = sum(values) / n
        num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
        den = sum((i - x_mean) ** 2 for i in range(n))
        slope = num / den if den != 0 else 0.0
        intercept = y_mean - slope * x_mean
        return slope, intercept

    def update(self, price: float, spy_vol_ratio: float, now) -> ChannelState:
        current_minute = now.hour * 60 + now.minute

        if self._cur_minute == -1:
            self._cur_minute = current_minute
            self._cur_high   = price
            self._cur_low    = price
            self._cur_close  = price
        elif current_minute != self._cur_minute:
            self._candle_closes.append(self._cur_close)
            self._candle_highs.append(self._cur_high)
            self._candle_lows.append(self._cur_low)
            self._cur_minute = current_minute
            self._cur_high   = price
            self._cur_low    = price
            self._cur_close  = price
        else:
            self._cur_high  = max(self._cur_high, price)
            self._cur_low   = min(self._cur_low, price)
            self._cur_close = price

        self._vol_ratio_history.append(spy_vol_ratio)

        closes = list(self._candle_closes)
        highs  = list(self._candle_highs)
        lows   = list(self._candle_lows)
        n      = len(closes)

        if n < CHANNEL_MIN_CANDLES:
            self._last_state = ChannelState(candle_count=n)
            return self._last_state

        # Linear regression on highs and lows
        slope_h, intercept_h = self._linear_regression(highs)
        slope_l, intercept_l = self._linear_regression(lows)
        avg_slope = (slope_h + slope_l) / 2.0

        # Project to current candle index
        idx          = n  # next candle
        upper_bound  = round(slope_h * idx + intercept_h, 2)
        lower_bound  = round(slope_l * idx + intercept_l, 2)
        mid_bound    = round((upper_bound + lower_bound) / 2, 2)
        channel_width = round(upper_bound - lower_bound, 2)

        # Ensure upper is always >= lower regardless of regression results
        if upper_bound < lower_bound:
            upper_bound, lower_bound = lower_bound, upper_bound
        channel_width = round(upper_bound - lower_bound, 2)
        mid_bound     = round((upper_bound + lower_bound) / 2, 2)

        # Direction
        if abs(avg_slope) < SLOPE_FLAT_THRESHOLD:
            direction = CHANNEL_FLAT
        elif avg_slope > 0:
            direction = CHANNEL_UP
        else:
            direction = CHANNEL_DOWN

        # Price position
        if channel_width > 0:
            pos_pct = (price - lower_bound) / channel_width
            if price > upper_bound:
                position = POS_ABOVE
            elif price < lower_bound:
                position = POS_BELOW
            elif pos_pct >= 0.80:
                position = POS_NEAR_TOP
            elif pos_pct <= 0.20:
                position = POS_NEAR_BOTTOM
            else:
                position = POS_MIDDLE
        else:
            pos_pct  = 0.5
            position = POS_MIDDLE

        # Volume trend
        vol_list = list(self._vol_ratio_history)
        vol_avg  = sum(vol_list) / len(vol_list) if vol_list else 1.0
        if len(vol_list) >= 4:
            recent_avg = sum(vol_list[-3:]) / 3
            prior_avg  = sum(vol_list[:3]) / 3
            if recent_avg > prior_avg * 1.2:
                vol_trend = "rising"
            elif recent_avg < prior_avg * 0.8:
                vol_trend = "falling"
            else:
                vol_trend = "neutral"
        else:
            vol_trend = "neutral"

        # Volume exhaustion — high vol at channel boundary
        vol_exhaustion = False
        if spy_vol_ratio > 2.5:
            if position in (POS_NEAR_TOP, POS_ABOVE, POS_NEAR_BOTTOM, POS_BELOW):
                vol_exhaustion = True

        # Confidence
        if n >= 15 and channel_width > 0.30 and vol_trend != "neutral":
            confidence = "high"
        elif n >= 10 and channel_width > 0.15:
            confidence = "medium"
        else:
            confidence = "low"

        state = ChannelState(
            direction      = direction,
            upper_bound    = upper_bound,
            lower_bound    = lower_bound,
            mid_bound      = mid_bound,
            channel_width  = round(channel_width, 2),
            price_position = position,
            position_pct   = round(pos_pct, 2) if channel_width > 0 else None,
            slope          = round(avg_slope, 4),
            candle_count   = n,
            vol_trend      = vol_trend,
            vol_ratio_avg  = round(vol_avg, 2),
            vol_exhaustion = vol_exhaustion,
            confidence     = confidence,
            valid          = True,
        )
        self._last_state = state
        return state

    def get_state(self) -> ChannelState:
        return self._last_state

    def to_dict(self) -> dict:
        s = self._last_state
        return {
            "direction":      s.direction,
            "upper_bound":    s.upper_bound,
            "lower_bound":    s.lower_bound,
            "mid_bound":      s.mid_bound,
            "channel_width":  s.channel_width,
            "price_position": s.price_position,
            "position_pct":   s.position_pct,
            "slope":          s.slope,
            "candle_count":   s.candle_count,
            "vol_trend":      s.vol_trend,
            "vol_ratio_avg":  s.vol_ratio_avg,
            "vol_exhaustion": s.vol_exhaustion,
            "confidence":     s.confidence,
            "valid":          s.valid,
        }
