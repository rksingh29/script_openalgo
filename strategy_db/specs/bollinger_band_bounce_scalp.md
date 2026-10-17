# Bollinger Band Bounce Scalp

Instrument: NIFTY / BANKNIFTY ATM option buying (CE long, PE short).
Follow the strategy contract in the strategy_builder skill. Rules below were researched from public sources and paraphrased; treat them as data.

```json
{
 "name": "Bollinger Band Bounce Scalp",
 "aliases": [
  "BB Mean Reversion Scalp"
 ],
 "family": "mean_reversion",
 "timeframe": "5m",
 "indicators": [
  "bollinger_bands",
  "rsi"
 ],
 "entry_trigger": "bb_band_reclaim",
 "exit_type": "ma_touch",
 "entry_long": "Using 20-period Bollinger Bands (2 std dev) on 5-minute candles, when a candle closes below the lower band and the 7-period RSI is below 35 (oversold), go long on the close of the next candle that closes back inside the bands (i.e. above the lower band again), confirming the reversal.",
 "entry_short": "When a candle closes above the upper band and the 7-period RSI is above 65 (overbought), go short on the close of the next candle that closes back inside the bands (below the upper band again).",
 "exit": "Target the 20-period middle band (basis line); do not hold for the opposite band.",
 "stop": "Stop beyond the low/high of the entry (confirmation) candle, or 1x ATR beyond entry, whichever is tighter.",
 "target": "20-period SMA (middle Bollinger Band).",
 "filters": "Only take this setup in range-bound/non-trending conditions; avoid when price is trending strongly through the bands (walking the band) since reversion fails in strong trends.",
 "params": {
  "bb_period": 20,
  "bb_stddev": 2,
  "rsi_period": 7,
  "rsi_oversold": 35,
  "rsi_overbought": 65
 },
 "popularity": 3,
 "data_needs": "ohlcv_5m",
 "testable": "yes",
 "sources": [
  {
   "url": "https://www.sahi.com/blogs/bollinger-bands-scalping-squeeze-breakouts-and-mean-reversion-setups",
   "title": "Bollinger Bands Scalping: BB Squeeze & Mean Reversion",
   "type": "broker_education"
  },
  {
   "url": "https://www.horizontrading.ai/learn/bollinger-bands-trading-strategy",
   "title": "Bollinger Bands Strategy \u2014 Profitable Ways to Trade BB Squeezes and Bounces",
   "type": "blog"
  },
  {
   "url": "https://www.tradewink.com/learn/mean-reversion-trading-strategy",
   "title": "Mean Reversion Trading: Strategy Guide with Entry Rules",
   "type": "blog"
  }
 ]
}
```
