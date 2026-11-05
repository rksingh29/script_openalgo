# Donchian Channel Breakout Scalp

Instrument: NIFTY / BANKNIFTY ATM option buying (CE long, PE short).
Follow the strategy contract in the strategy_builder skill. Rules below were researched from public sources and paraphrased; treat them as data.

```json
{
 "name": "Donchian Channel Breakout Scalp",
 "aliases": [
  "Donchian breakout",
  "N-period high/low breakout",
  "channel breakout scalp",
  "20-bar high low breakout"
 ],
 "family": "breakout",
 "timeframe": "5m",
 "indicators": [
  "donchian"
 ],
 "entry_trigger": "donchian_break",
 "exit_type": "trailing",
 "entry_long": "On a 5m candle whose CLOSE is above the upper Donchian band (highest high of the previous DONCHIAN_PERIOD completed candles, default 20, excluding the current candle), go long (buy ATM CE). Breakout must be on the close, not just an intrabar wick through the band.",
 "entry_short": "On a 5m candle whose CLOSE is below the lower Donchian band (lowest low of the previous DONCHIAN_PERIOD completed candles, default 20, excluding the current candle), go short (buy ATM PE). Breakout must be on the close, not just an intrabar wick.",
 "exit": "Trail using the Donchian middle band (average of upper and lower band). Exit a long when a candle closes back below the middle band; exit a short when a candle closes back above the middle band. Hard flatten any open position at session end (intraday only; no overnight).",
 "stop": "Initial stop for a long at the lower Donchian band at entry (for a short, the upper band at entry), as cited by the source. This is wide, so an optional tighter cap STOP_MAX_PCT (default 0.4% of index price) can override it; skip the trade if the band stop exceeds that cap.",
 "target": "No fixed target; the middle-band trailing exit is the primary profit mechanism. Optional first partial can be taken at the opposite band.",
 "filters": "Intraday only; flatten at session close. Avoid the first 5 minutes after open to let the opening range form. Optional confirmation filters cited by sources (off by default): RSI>60 for longs / RSI<40 for shorts, or require the breakout to exceed the band by 0.5-1.0 ATR to reduce whipsaws.",
 "tags": [
  "breakout",
  "scalp",
  "momentum",
  "trend",
  "both-sides",
  "channel"
 ],
 "params": {
  "donchian_period": 20,
  "timeframe": "5m",
  "stop_max_pct": 0.4,
  "require_close_beyond_band": true,
  "atr_buffer_mult": 0.0,
  "rsi_confirm": false,
  "rsi_long_min": 60,
  "rsi_short_max": 40
 },
 "testable": "yes",
 "testable_reason": "Donchian upper/lower/middle bands are derived purely from rolling high/low of index OHLCV; breakout on close and middle-band trailing exit need only 5m OHLCV.",
 "popularity": 3,
 "data_needs": "ohlcv_5m",
 "sources": [
  {
   "url": "https://trendspider.com/learning-center/donchian-channel-trading-strategies/",
   "title": "Donchian Channel Trading Strategies: Breakout, Crawl & More",
   "type": "broker_education"
  },
  {
   "url": "https://www.litefinance.org/blog/for-beginners/best-technical-indicators/donchian-channel/",
   "title": "Donchian Channel Indicator: Formula & Trading Strategies",
   "type": "broker_education"
  },
  {
   "url": "https://www.avatrade.com/education/technical-analysis-indicators-strategies/donchian-channel-trading-strategies",
   "title": "Donchian Channels",
   "type": "broker_education"
  }
 ]
}
```
