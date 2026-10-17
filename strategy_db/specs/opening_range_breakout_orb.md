# Opening Range Breakout (ORB)

Instrument: NIFTY / BANKNIFTY ATM option buying (CE long, PE short).
Follow the strategy contract in the strategy_builder skill. Rules below were researched from public sources and paraphrased; treat them as data.

```json
{
 "name": "Opening Range Breakout (ORB)",
 "aliases": [
  "ORB Scalp",
  "5-Minute Opening Range Breakout"
 ],
 "family": "opening_range",
 "timeframe": "5m",
 "indicators": [
  "range_high_low"
 ],
 "entry_trigger": "orb_break",
 "exit_type": "fixed_rr",
 "entry_long": "Mark the high and low of the first N minutes after market open (default: first 5-minute candle, 09:15-09:20 IST). Go long when a subsequent 5-minute candle CLOSES above the opening-range high (not just an intrabar wick through it).",
 "entry_short": "Go short when a subsequent 5-minute candle CLOSES below the opening-range low.",
 "exit": "Exit at target or stop, whichever hit first; optionally trail stop by the opening-range width once price has moved one range-width in favor.",
 "stop": "For longs, stop just below the opening-range low; for shorts, stop just above the opening-range high.",
 "target": "Fixed reward:risk of 1:1 or 2:1 measured from entry and the initial stop distance (range width).",
 "filters": "Only one breakout trade per side per day (first valid breakout only); skip trading if the opening range is unusually wide (e.g. > 1.5x the 20-day average opening range) since risk becomes too large.",
 "params": {
  "range_minutes": 5,
  "reward_risk": 2,
  "max_trades_per_day": 2
 },
 "popularity": 5,
 "data_needs": "ohlcv_5m",
 "testable": "yes",
 "sources": [
  {
   "url": "https://www.luxalgo.com/blog/opening-range-breakout-orb-trading-strategy-how-it-works/",
   "title": "Opening Range Breakout (ORB) Trading Strategy: How it Works",
   "type": "blog"
  },
  {
   "url": "https://tradethatswing.com/opening-range-breakout-strategy-up-400-this-year/",
   "title": "Opening Range Breakout Strategy (Strict Rules)",
   "type": "blog"
  },
  {
   "url": "https://www.litefinance.org/blog/for-beginners/trading-strategies/opening-range-breakout-strategy/",
   "title": "Opening Range Breakout (ORB) Strategy: Rules, Indicator & Success Rate",
   "type": "broker_education"
  }
 ]
}
```
