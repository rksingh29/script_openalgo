# Manual2 (user, 2026-10-29)

Based on nifty_direction_ce_pe.py (same entry: 3 rising 5-min readings of the ATM CE or PE). User's changes:
1. Book the profit at every +1000 (PROFIT_TARGET), then check whether to continue the same side or change the side:
   a fresh reading + the last 2 rises (CONTINUE_READINGS) -> same option still rising = buy it again (fresh ATM),
   the other option rising = buy that one, neither = normal new cycle.
2. No multi-leg: no reversal leg, no riding past the target.
3. Exit a wrong entry at -1000 (MAX_LOSS).
4. No new trades 11:00-12:45 (BLACKOUT_START / BLACKOUT_END); an open trade keeps its target and stop
   (BLACKOUT_EXIT = True exits it at 11:00).
Profit and loss are variables (NIFTY 1000 / 1000; save other values per instrument in the options panel).
Checks every 15 s; max 8 trades a day, daily loss 3000.

## 2026-10-29 changes after the first live run
- Exit near support / resistance (SR_EXIT pivots: previous day's pivot, CPR, R1-R3, S1-S3, high, low), only in profit.
- No entry when the next level in the trade's direction is closer than 30 points (SR_MIN_ROOM).
- At +1000 with the direction holding: keep the trade and lock a floor (+500, then +1500 at +2000, ...) instead of
  selling and buying the same strike again (AFTER_TARGET hold; "rebook" = the original rule).
- The continue check no longer counts a reading taken seconds after the previous one (FRESH_READING_MIN 2).
