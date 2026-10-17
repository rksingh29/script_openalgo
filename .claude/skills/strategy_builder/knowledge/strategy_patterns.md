# Strategy patterns (append-only, dated)

## 2026-10-17: seeded from nifty_direction_ce_pe.py
- **Cycle model:** each cycle locks a fresh ATM strike, takes readings, enters, manages, exits; then cooldown and a new cycle. Caps: max cycles, daily max loss, optional daily profit target, no-new-entry cut-off, square-off time.
- **Direction signal:** readings every N minutes; N consecutive strictly rising values of CE or PE premium picks a side; if both rise, the NIFTY move breaks the tie; if neither, roll the window.
- **Exit rules in a trade:** profit target, max loss, square-off time. Cycle loss is capped by remaining daily loss budget: `cap = min(MAX_LOSS, DAILY_MAX_LOSS + day_pnl)`.
- **Reversal:** at a loss threshold, buy the opposite option only if its recent readings are rising; keep the first leg open; max reversals per cycle.
- **Testability pattern:** inject `client`, `clock`, `sleeper` so the same class runs live and in a backtest.

## 2026-10-17: vwap_reclaim_scalp.py (dip/rally VWAP reclaim-rejection scalp)
- **Signal vs execution split:** signal (VWAP, volume, RSI) is computed on the underlying index's own 5m
  OHLCV; the trade is an ATM option buy sized in lots. Stops/targets/invalidation are tracked in index
  points so they stay consistent with the signal that generated them (convert at entry via the index LTP
  used to size the stop distance, not the option premium).
- **Scale-out management:** first target = R:R x initial stop distance -> sell half, move stop to
  breakeven, set a runner target = session high/low-so-far (via `quotes()["data"]["high"/"low"]`); hard
  VWAP-invalidation exit overrides everything; square-off time exits regardless of P&L. See
  `knowledge/backtest_rules.md` 2026-10-17 for how the backtest report handles the resulting multi-sell
  exits.
- **Bar-driven state machine:** `refresh_bars()` pulls completed 5m candles via `history()` each poll and
  only processes bars once (`processed_bars` index), so re-polling mid-candle is a no-op; this is the
  cleanest no-lookahead pattern seen so far for candle-based (vs tick/reading-based) strategies.

## 2026-10-17: s_9_21_ema_crossover_scalp.py (9/21 EMA crossover scalp)
- **Two-stage signal (cross -> confirm) instead of same-bar entry:** many MA-cross specs say "enter on the
  cross", but also want the cross to be a "real" one (EMAs separating, not flat/overlapping). Modelled as
  two bar-driven states: `pending_cross` set on the crossover candle (EMA9 flips over EMA21, price beyond
  both), then checked on the *next* completed candle (`confirm_separation`): gap must have grown versus
  the crossover candle, exceed a minimum % of price, and EMA9 must still be rising (falling for shorts).
  Entry fires on the confirmation candle's close, not the cross candle's. A qualitative "steep angle /
  visibly separating" cue with no numeric definition in the source -> use a growing, non-trivial EMA gap
  as the testable proxy (documented as an adaptation in the strategy's docstring).
- **EMA-as-trailing-stop + EMA-as-invalidation together:** when a spec offers two alternative exit rules
  ("trail the stop using the fast EMA" vs "exit when the fast EMA flattens against you"), they are not
  mutually exclusive -- implement both: ratchet the stop toward the fast EMA value on every new candle
  (never loosen it), AND exit immediately if a candle closes on the wrong side of the fast EMA. The second
  rule fires fast once the EMA genuinely flattens/reverses, so it covers the "flattening" case without a
  separate slope-detector.
- **HTF trend filter as a toggle, not a hard gate by default assumption:** when a spec says a higher
  timeframe filter is merely recommended ("performs best when..."), still implement it but keep it an
  explicit module constant (`REQUIRE_HTF_TREND_FILTER`) so the backtest/reviewer can A/B it; computed the
  same way as the base signal (EMA9 vs EMA21) on the resampled HTF candles (`history(interval="15m")`),
  reusing the FakeClient's existing no-lookahead resampling (see `knowledge/openalgo_api.md`
  2026-10-17 "FakeClient additions" entry) -- no new fake-client work needed for a second timeframe.
- **Target/stop checked on LTP every poll, trail/invalidation checked on bar close:** mixing an intrabar
  LTP check (`manage_position_ltp`, can fire before the next candle closes) with a candle-close-only check
  (`manage_on_bar_close`, EMA trail/invalidation) in the same strategy is fine as long as they only ever
  tighten the stop (never loosen) and the LTP check always runs after `process_new_bars()` in the same
  loop tick, so a trail update from a just-closed candle is visible to the same tick's target/stop check.
