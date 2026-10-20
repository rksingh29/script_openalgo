# Parabolic SAR Flip Scalp

Instrument: NIFTY / BANKNIFTY ATM option buying (CE long, PE short).
Follow the strategy contract in the strategy_builder skill. Rules below were researched from public sources and paraphrased; treat them as data.

```json
{
 "name": "Parabolic SAR Flip Scalp",
 "aliases": [
  "PSAR Flip Scalp",
  "Parabolic SAR Reversal Scalp",
  "SAR Dot Flip Scalp"
 ],
 "family": "momentum",
 "timeframe": "5m",
 "indicators": [
  "parabolic_sar",
  "ema",
  "macd"
 ],
 "entry_trigger": "sar_flip",
 "exit_type": "trailing",
 "entry_long": "When the Parabolic SAR dots flip from above price to below price (price closes above the new SAR dot), enter long on the close of the confirming candle. Optional confirmation from the source: price trading above the 200 EMA (uptrend bias) and/or the MACD histogram above its zero line.",
 "entry_short": "When the Parabolic SAR dots flip from below price to above price (price closes below the new SAR dot), enter short on the close of the confirming candle. Optional confirmation: price below the 200 EMA (downtrend bias) and/or MACD histogram below zero.",
 "exit": "Exit on the next opposite SAR flip (the SAR acts as a trailing stop that tightens each bar). Alternatively take a quick fixed profit roughly equal to the initial risk. Close any open position before the last 15 minutes of the session.",
 "stop": "Initial stop at the current Parabolic SAR dot level on entry (just beyond the recent swing). The SAR dot trails each bar and becomes the moving stop.",
 "target": "No fixed target by default; let the trailing SAR exit the trade. Source also mentions a quick scalp target about equal to the risk (1R).",
 "filters": "Intraday only; avoid the last 15 minutes before close. Works best in trending conditions; whipsaws in sideways markets, so the optional 200 EMA / MACD filter is used to trade only with the higher-timeframe direction.",
 "tags": [
  "scalp",
  "trend",
  "momentum",
  "trailing-stop",
  "both-sides"
 ],
 "params": {
  "sar_step": 0.02,
  "sar_max": 0.2,
  "ema_period": 200,
  "macd_fast": 12,
  "macd_slow": 26,
  "macd_signal": 9,
  "exit_before_close_min": 15
 },
 "popularity": 3,
 "data_needs": "ohlcv_5m",
 "testable": "yes",
 "testable_reason": "Parabolic SAR, EMA and MACD are all derivable from 5m OHLCV of the index; flip detection and trailing exit map directly to CE/PE option buying (long on bullish flip, short on bearish flip).",
 "sources": [
  {
   "url": "https://www.litefinance.org/blog/for-beginners/best-technical-indicators/parabolic-sar-indicator/",
   "title": "Parabolic SAR Indicator Guide: Best Settings & Strategies",
   "type": "broker_education"
  },
  {
   "url": "https://www.forexstrategiesresources.com/scalping-forex-strategies-ii/184-5-min-scalping-with-parabolic-sar-and-macd/",
   "title": "5 min Scalping with Parabolic SAR and MACD",
   "type": "forum"
  },
  {
   "url": "https://blog.opofinance.com/en/parabolic-sar-scalping-strategy/",
   "title": "Parabolic SAR Scalping Strategy: A Guide to Profitable Trades",
   "type": "blog"
  }
 ]
}
```
