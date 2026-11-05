# Manual1 (user, 2026-10-28)

Source: the user's manual NIFTY trades on 2026-10-28 (another account, 03NOV26 weekly), "looking at the market range,
confirmed with option-chain open interest". Rebuilt from the order list:

- 09:39-10:22: PEs sold (22600/22700/22500/22400) as NIFTY fell to 22,498; the low held exactly on the biggest PE OI
  (22500). Covered ~20 % cheaper at 10:55 on the bounce.
- 11:26-12:30: PEs sold again on the rise (22700/22400/22800), covered 12:50-13:01 near the top.
- 13:02-13:46: CEs sold (22800/22850/22700/22600) at the day's high 22,778 (13:00) under the 22800 CE OI wall.
- 14:26-15:26: range 22,670-22,740 between the walls; both sides short, 22700 PE and 22800 CE carried overnight (NRML).

Rules as coded (strategies/manual1.py):
- Walls on every closed 5m candle from the option chain within 300 pts: support = max PE OI strike at/below the index,
  resistance = max CE OI strike at/above it.
- SELL PE (OTM1): candle low within 30 pts of support, close >= support - 20, up candle (close > open and > previous
  close), PE OI at the wall >= 1.2x CE OI there and not down > 5 % since the last reading. SELL CE: mirror.
- One short per side (both together = strangle). Exit: premium -20 % (target), +35 % or -2500 rupees (stop), a 5m close
  20 pts beyond the wall, 15:15. Entries 09:30-14:45, 6 trades/day, daily loss 4000.
- Not modelled: scaling in as the premium rose, the long-option scalps 13:24-13:44, overnight carry.

## v2 (2026-10-29)
User: "enter with 1 lot then keep increasing if the direction is true; be ready for a buy position if the trade goes
wrong; MACD helps the timing; I get unlimited losses, that I want to improve." Trade books 26-28 Oct are from Zerodha.
- RESELL on: after a target, sell the same side again on a pullback-bounce candle while the level holds.
- PYRAMID on: 1 lot, +1 lot per further 10 % fall in the premium while the 5m MACD agrees (max 5); exit on a 15 %
  bounce off the premium low, a MACD cross against, the level breaking, or the whole-position loss limit (2500).
- FAST_BREAK_EXIT on: cover as soon as NIFTY trades 20 points through the level.
- Off, available: MACD_CONFIRM for entries, HEDGE wing / next_expiry, REVERSE_ON_BREAK (buy in the break's direction).
- Never overnight, never adds to a losing short.
