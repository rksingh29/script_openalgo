# Supertrend Flip Scalp

Instrument: NIFTY / BANKNIFTY ATM option buying (CE long, PE short).
Follow the strategy contract in the strategy_builder skill. Rules below were researched from public sources and paraphrased; treat them as data.

```json
{
 "name": "Supertrend Flip Scalp",
 "aliases": [
  "Supertrend Trailing Scalp"
 ],
 "family": "supertrend",
 "timeframe": "5m",
 "indicators": [
  "supertrend",
  "atr"
 ],
 "entry_trigger": "supertrend_flip",
 "exit_type": "trailing",
 "entry_long": "Compute Supertrend with ATR period 7-10 and multiplier 2-3 on 5-minute candles. Go long on the candle close where the Supertrend flips from red (above price) to green (below price), i.e. price closes above the Supertrend line for the first time after being below it.",
 "entry_short": "Go short on the candle close where the Supertrend flips from green to red, i.e. price closes below the Supertrend line for the first time after being above it.",
 "exit": "Exit (and reverse bias) when price closes back through the Supertrend line on the opposite side - the line itself acts as the exit signal, no separate target.",
 "stop": "Initial stop at the Supertrend line value at entry; trail the stop along the Supertrend line as it moves in the trade's favor.",
 "target": "No fixed target; ride the trend until the Supertrend line flips (trailing exit).",
 "filters": "Works best on trending days; many sources pair it with a momentum filter (e.g. only take Supertrend signals aligned with a higher-timeframe trend) to reduce whipsaws in choppy conditions.",
 "params": {
  "atr_period": 10,
  "multiplier": 3
 },
 "popularity": 4,
 "data_needs": "ohlcv_5m",
 "testable": "yes",
 "sources": [
  {
   "url": "https://trendspider.com/learning-center/supertrend-indicator-a-comprehensive-guide/",
   "title": "SuperTrend Indicator: Comprehensive Guide to Signals & Settings",
   "type": "blog"
  },
  {
   "url": "https://www.netpicks.com/supertrend-indicator/",
   "title": "Supertrend Settings: Exact Rules, Backtest, and Chart Examples",
   "type": "blog"
  },
  {
   "url": "https://www.mql5.com/en/blogs/post/763976",
   "title": "Best SuperTrend Settings for 1-Minute Chart",
   "type": "forum"
  }
 ]
}
```
