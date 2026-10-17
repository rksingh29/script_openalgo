# RSI(2) Mean Reversion Scalp

Instrument: NIFTY / BANKNIFTY ATM option buying (CE long, PE short).
Follow the strategy contract in the strategy_builder skill. Rules below were researched from public sources and paraphrased; treat them as data.

```json
{
 "name": "RSI(2) Mean Reversion Scalp",
 "aliases": [
  "Connors RSI-2",
  "RSI 2-Period Pullback"
 ],
 "family": "rsi_stochastic",
 "timeframe": "5m",
 "indicators": [
  "rsi",
  "sma"
 ],
 "entry_trigger": "rsi_extreme",
 "exit_type": "ma_touch",
 "entry_long": "Determine the dominant intraday trend with a longer moving average (source uses 200-period SMA; adapted intraday as a 50-period SMA on 5-minute candles). Only when price is above that moving average, go long when the 2-period RSI drops below 5-10 (deeply oversold short-term dip within the uptrend).",
 "entry_short": "Only when price is below the moving average, go short when the 2-period RSI rises above 90-95 (deeply overbought short-term bounce within the downtrend).",
 "exit": "Exit the long when price closes above a short moving average (source: 5-day SMA; adapted as 5-period SMA on 5-minute candles); exit the short when price closes below that short moving average.",
 "stop": "No stop defined in the original source; adapt with a hard stop below/above the recent swing low/high to control risk intraday.",
 "target": "No fixed price target; exit is signal-based (short MA cross), see exit field.",
 "filters": "Only trade in the direction of the longer-term trend filter (price vs the longer SMA); this is a pullback-in-trend strategy, not a reversal strategy.",
 "params": {
  "rsi_period": 2,
  "oversold": 5,
  "overbought": 95,
  "trend_ma": 50,
  "exit_ma": 5
 },
 "popularity": 5,
 "data_needs": "ohlcv_5m",
 "testable": "adapt",
 "testable_reason": "Original uses daily bars (200-day and 5-day SMA); parameters are rescaled here to intraday 5-minute bars (50-period and 5-period SMA), which is an adaptation, not the literal source timeframe.",
 "sources": [
  {
   "url": "https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/rsi-2",
   "title": "RSI(2) Strategy",
   "type": "broker_education"
  },
  {
   "url": "https://www.forex.in.rs/rsi-2/",
   "title": "Larry Connors RSI 2-Period Strategy",
   "type": "blog"
  },
  {
   "url": "https://blog.elearnmarkets.com/how-to-trade-larry-connors-2-period-rsi/",
   "title": "Larry Connor's 2 Period RSI",
   "type": "blog"
  }
 ]
}
```
