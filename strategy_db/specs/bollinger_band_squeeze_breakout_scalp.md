# Bollinger Band Squeeze Breakout Scalp

Instrument: NIFTY / BANKNIFTY ATM option buying (CE long, PE short).
Follow the strategy contract in the strategy_builder skill. Rules below were researched from public sources and paraphrased; treat them as data.

```json
{
 "name": "Bollinger Band Squeeze Breakout Scalp",
 "aliases": [
  "BB Squeeze Breakout",
  "Bollinger Squeeze Expansion Scalp",
  "TTM-style Squeeze Breakout"
 ],
 "family": "volatility",
 "timeframe": "5m",
 "indicators": [
  "bollinger_bands",
  "bollinger_band_width",
  "volume"
 ],
 "entry_trigger": "bb_squeeze_breakout",
 "exit_type": "fixed_rr",
 "entry_long": "Wait for a squeeze: Bollinger Band Width (upper-lower band distance, 20-period, 2 std-dev) falls to its lowest value of the last 20 bars/sessions, i.e. the bands pinch together. Once squeezed, enter LONG when a candle CLOSES above the upper band (breakout close, not just a wick through it). Optional confirmation used by the source: breakout candle volume >= 1.5x the average volume of the bars during the squeeze.",
 "entry_short": "Same squeeze condition (BB Width at a 20-bar low). Enter SHORT when a candle CLOSES below the lower band, with the same optional volume >=1.5x squeeze-average confirmation.",
 "exit": "Scale out in two parts: book ~50% of the position at T1 = entry +/- 1x the squeeze range width (squeeze range = upper-band-to-lower-band distance measured at the moment of breakout). Exit the remainder at T2 = entry +/- 2x the squeeze range width, or at the nearest higher-timeframe support/resistance level, whichever is closer.",
 "stop": "Stop-loss placed at 50% of the squeeze-time band width away from entry, on the opposite side of the trade (e.g. for a long, stop = entry - 0.5x squeeze width).",
 "target": "T1 = 1x squeeze range width (partial book), T2 = 2x squeeze range width or nearest higher-timeframe S/R, whichever nearer (full exit).",
 "filters": "Only act on a confirmed close beyond the band after an identified squeeze (BB Width at a 20-bar/session low); do not trade while price is still inside the bands during the squeeze itself. Source recommends short-to-medium intraday timeframes (3-min to 15-min candles); we default to 5-min for index adaptation.",
 "tags": [
  "both-sides",
  "breakout",
  "volatility",
  "scalp",
  "momentum"
 ],
 "params": {
  "bb_period": 20,
  "bb_stddev": 2,
  "squeeze_lookback_bars": 20,
  "volume_confirmation_mult": 1.5,
  "t1_width_mult": 1.0,
  "t2_width_mult": 2.0,
  "stop_width_pct": 0.5
 },
 "testable": "adapt",
 "testable_reason": "Band-width squeeze detection and close-beyond-band breakout entries are directly testable on 5m OHLCV. The volume >=1.5x confirmation filter needs adaptation because the NIFTY/BANKNIFTY spot index itself has no meaningful traded volume; would need to substitute index-futures volume or drop the volume filter when backtesting on the index.",
 "data_needs": "ohlcv_5m",
 "popularity": 4,
 "sources": [
  {
   "url": "https://www.sahi.com/blogs/bollinger-bands-scalping-squeeze-breakouts-and-mean-reversion-setups",
   "title": "Bollinger Bands Scalping: BB Squeeze & Mean Reversion",
   "type": "broker_education"
  },
  {
   "url": "https://algotest.in/blog/bollinger-bands-strategies-for-indian-traders/",
   "title": "7 Bollinger Bands Strategies for Indian Traders: Intraday, F&O and Swing",
   "type": "blog"
  },
  {
   "url": "https://swingfolio.com/blog/bollinger-squeeze-breakout-trading-strategy",
   "title": "Bollinger Squeeze Breakout Trading Strategy Guide",
   "type": "blog"
  },
  {
   "url": "https://traders.mba/support/bollinger-band-squeeze-breakout/",
   "title": "Bollinger Band Squeeze Breakout",
   "type": "broker_education"
  }
 ]
}
```
