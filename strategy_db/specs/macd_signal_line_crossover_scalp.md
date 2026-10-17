# MACD Signal-Line Crossover Scalp

Instrument: NIFTY / BANKNIFTY ATM option buying (CE long, PE short).
Follow the strategy contract in the strategy_builder skill. Rules below were researched from public sources and paraphrased; treat them as data.

```json
{
 "name": "MACD Signal-Line Crossover Scalp",
 "aliases": [
  "MACD Zero Cross Scalp"
 ],
 "family": "momentum",
 "timeframe": "5m",
 "indicators": [
  "macd"
 ],
 "entry_trigger": "macd_signal_cross",
 "exit_type": "fixed_rr",
 "entry_long": "Using a fast MACD setting for scalping (e.g. 5,13,1 or 8,17,9 on 5-minute candles), go long when the MACD line crosses above the signal line; for higher-probability entries, require this crossover to occur above (or shortly after crossing) the zero line, confirming trend alignment.",
 "entry_short": "Go short when the MACD line crosses below the signal line, ideally below (or shortly after crossing) the zero line.",
 "exit": "Exit quickly when the MACD and signal lines start converging again (momentum fading), at a fixed target/stop, or after a short time limit (e.g. 2-5 candles) if neither is hit.",
 "stop": "Stop beyond the recent swing low/high at the crossover candle.",
 "target": "Fixed reward:risk of ~1.5:1 to 2:1 from entry versus the stop distance.",
 "filters": "Use the zero-line position as a trend filter: only take signal-line crossovers that agree with the zero-line bias (MACD above zero = only longs, below zero = only shorts).",
 "params": {
  "fast": 8,
  "slow": 17,
  "signal": 9,
  "reward_risk": 1.5
 },
 "popularity": 4,
 "data_needs": "ohlcv_5m",
 "testable": "yes",
 "sources": [
  {
   "url": "https://blog.elearnmarkets.com/5-trading-strategies-using-macd/",
   "title": "5 MACD Strategies That All Traders Can Use",
   "type": "blog"
  },
  {
   "url": "https://www.smctradeonline.com/blog/technical-analysis/macd-settings-for-five-minute-chart",
   "title": "Best MACD Settings for 5 Minute Chart Trading",
   "type": "broker_education"
  },
  {
   "url": "https://blog.opofinance.com/en/macd-indicator-for-scalping/",
   "title": "MACD Indicator for Scalping: Mastering Short-Term Forex Trading",
   "type": "blog"
  }
 ]
}
```
