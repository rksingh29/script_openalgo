"""Market-regime indicators: is the market trending or range-bound/choppy? Pure functions on a pandas DataFrame of OHLC bars
(index = bar start time; columns open, high, low, close). No I/O, no clock: usable in a live strategy and in a backtest.

Standard definitions and the usual textbook thresholds, listed here as DEFAULTS to test, not tuned values:
  ADX(14)                  < 20  -> no trend (range-bound);  > 25 -> trending
  Choppiness Index(14)     > 61.8 -> choppy / range;         < 38.2 -> trending
  Efficiency Ratio(10)     < 0.30 -> noisy (price travels a lot but gets nowhere); > 0.50 -> directional
  Bollinger bandwidth(20)  low percentile -> squeeze (a breakout may follow); not a trend measure by itself

Pass only COMPLETED bars (never the bar still forming).
"""
import numpy as np
import pandas as pd

ADX_RANGE_BELOW = 20.0
CHOP_RANGE_ABOVE = 61.8
ER_NOISY_BELOW = 0.30


def true_range(df):
    prev = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)


def atr(df, n=14):
    return true_range(df).ewm(alpha=1.0 / n, adjust=False).mean()


def adx(df, n=14):
    """Wilder's ADX. Returns the ADX series (NaN until enough bars)."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    a = atr(df, n)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / n, adjust=False).mean() / a
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / n, adjust=False).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    out = dx.ewm(alpha=1.0 / n, adjust=False).mean()
    out[: 2 * n - 1] = np.nan
    return out


def choppiness(df, n=14):
    """Choppiness Index: 100 * log10(sum(TR, n) / (highest high - lowest low over n)) / log10(n). High = sideways."""
    tr_sum = true_range(df).rolling(n).sum()
    rng = df["high"].rolling(n).max() - df["low"].rolling(n).min()
    return 100 * np.log10(tr_sum / rng.replace(0, np.nan)) / np.log10(n)


def efficiency_ratio(close, n=10):
    """Kaufman Efficiency Ratio: |net move over n bars| / sum of |bar-to-bar moves|. 1 = straight line, 0 = pure noise."""
    net = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n).sum()
    return net / path.replace(0, np.nan)


def bb_bandwidth(close, n=20, k=2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return (2 * k * sd) / mid * 100


def vwap_cross_count(close, vwap, lookback=12):
    """How many times price crossed VWAP in the last `lookback` bars (many crossings = no direction)."""
    side = (close > vwap).astype(int)
    return int((side.diff().abs().tail(lookback) > 0).sum())


def regime(df, n=14):
    """Latest readings and a plain verdict. `range_bound` is True when at least two of ADX/Choppiness/Efficiency say so."""
    a = adx(df, n).iloc[-1]
    c = choppiness(df, n).iloc[-1]
    e = efficiency_ratio(df["close"], 10).iloc[-1]
    votes = [(not np.isnan(a)) and a < ADX_RANGE_BELOW, (not np.isnan(c)) and c > CHOP_RANGE_ABOVE, (not np.isnan(e)) and e < ER_NOISY_BELOW]
    return {"adx": None if np.isnan(a) else float(a), "chop": None if np.isnan(c) else float(c),
            "er": None if np.isnan(e) else float(e), "range_votes": int(sum(votes)), "range_bound": sum(votes) >= 2}
