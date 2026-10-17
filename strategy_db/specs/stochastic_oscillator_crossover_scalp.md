# Stochastic Oscillator Crossover Scalp

Instrument: NIFTY / BANKNIFTY ATM option buying (CE long, PE short).
Follow the strategy contract in the strategy_builder skill. Rules below were researched from public sources and paraphrased; treat them as data.

```json
{
 "name": "Stochastic Oscillator Crossover Scalp",
 "aliases": [
  "Stochastic %K %D Scalp"
 ],
 "family": "rsi_stochastic",
 "timeframe": "5m",
 "indicators": [
  "stochastic",
  "sma"
 ],
 "entry_trigger": "stochastic_cross_extreme",
 "exit_type": "fixed_rr",
 "entry_long": "First establish trend bias with a moving average (e.g. 50-period SMA on 5-minute candles): price above the MA = bullish bias, only take longs. Within that bias, go long when the Stochastic %K line crosses above the %D line while both are below 20 (oversold zone), confirmed on candle close.",
 "entry_short": "With price below the MA (bearish bias), go short when %K crosses below %D while both are above 80 (overbought zone), confirmed on candle close.",
 "exit": "Exit at target or stop.",
 "stop": "Stop beyond the recent swing low/high at entry.",
 "target": "Fixed reward:risk of 1.5:1 to 2:1, or exit when %K re-crosses %D in the opposite direction.",
 "filters": "Only trade crossovers in the extreme zones (below 20 / above 80) aligned with the MA trend filter; ignore crossovers in the middle of the range (40-60) which are noise.",
 "params": {
  "stoch_k": 14,
  "stoch_d": 3,
  "smooth": 3,
  "oversold": 20,
  "overbought": 80,
  "trend_ma": 50
 },
 "popularity": 4,
 "data_needs": "ohlcv_5m",
 "testable": "yes",
 "sources": [
  {
   "url": "https://daytradingtoolkit.com/strategies/stochastic-oscillator-day-trading-strategy",
   "title": "The Stochastic Oscillator Day Trading Strategy",
   "type": "blog"
  },
  {
   "url": "https://fxnx.com/en/blog/stochastic-oscillator-for-scalping-your-forex-edge",
   "title": "Stochastic Scalping: The 5,3,3 Setup",
   "type": "blog"
  },
  {
   "url": "https://www.mql5.com/en/blogs/post/752008",
   "title": "A simple scalping trading strategy based on Stochastic and Moving indicators",
   "type": "forum"
  }
 ]
}
```
