# All_in_one (user, 2026-10-29)

Source: an AI-generated brief (multi-strategy intraday system by session, sequential session state, central risk
manager, NO TRADE as a valid decision). v1 is a deterministic, auditable baseline inside one strategy file.

Sessions and default plays (configurable: S1_PLAYS ... S5_PLAYS, "none" = NO TRADE):
- S1 09:15-09:30 discovery only: opening range, gap vs previous close, previous day's pivot levels.
- S2 09:30-11:00 orb (5m close beyond the OR + 5 pts, until 10:30, invalid below/above the OR middle) and
  direction (3 rising 5-min ATM premium readings, clock grid from the session start).
- S3 11:00-13:00 NO TRADE.  S4 13:00-14:00 direction.  S5 14:00-15:15 NO TRADE, square-off 15:15.
Evidence for the defaults: trade journal 2026-10-29 (842 backtested trades, 85 instrument-days, net after costs):
premium-direction S2 +14.6k, S3 -42.7k, S4 +12.0k, S5 -15.6k; ORB S2 +7.5k (14 trades); trend and mean-reversion
families lost in S3-S4. In-sample, one look: hypotheses.

Session reports: logged at every session change (open/high/low/close, efficiency-ratio regime trend/mixed/range,
bias, trades, P&L); S1's report feeds the ORB; MORNING_BIAS_FILTER (off) lets S4 trade only with the S2 bias.
Risk checks before every entry: one position, 3 trades per session, 6 a day, daily loss 3000, 3 losses in a row,
20 min before square-off, 30 pts room to the next pivot level. Exits: -1000, hold at +1000 with a +500 floor,
pivot level near while in profit, the ORB invalidation, 15:15. Option buying only; never overnight.
Not done (from the brief): web research report with references, walk-forward / out-of-sample split (too few days:
~29 NIFTY days cached), breadth/sector data, defined-risk spreads, persisted state across a restart (the trade
manager's ledger persists trades; the strategy's own session state does not).
