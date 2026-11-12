# Episode metrics catalogue (metrics_version 1)

Source: `apps/intelligence/services/metrics.py`. Flat dotted keys, stored in `intel_episode_metrics.metrics`. Lessons and citations (Phases 3 and 5) may only use these keys. Bump `METRICS_VERSION` on any definition change; episodes built with an older version are recomputed by the next sweep or `backfill_episodes`. Everything is deterministic Python; no LLM is involved.

Absent keys mean "not measurable for this episode" (never zero-filled). Money values are floats in the episode currency (`outcome.currency`); never add them across currencies, use the bps keys instead.

## Common (stock and option)

| Key | Unit | Definition |
|---|---|---|
| outcome.gross_pnl / commission / net_pnl | currency | Realised P&L (weighted-average cost, engine arithmetic), commissions, gross minus commissions. For an OPEN episode: realised so far. |
| outcome.currency | text | Currency of the fills. |
| outcome.entry_notional | currency | Sum of entry quantity x price x multiplier. |
| outcome.cost_bps | bps | commission / entry_notional x 1e4. |
| outcome.net_return_bps | bps | net_pnl / entry_notional x 1e4. |
| outcome.risk_basis | currency (text) | Sizing risk budget (stocks, when a sizing decision exists) or the option structure's max loss. Divisor of `return_on_risk`. |
| outcome.is_closed | bool | |
| outcome.hold_seconds / hold_bars | s / bars | Close minus open; bars = seconds / timeframe length. |
| exit.reason / exit.subtype | enum / text | SIGNAL, SOFT_EXIT (+ subtype), EXPIRY_RULE, HOLD_PERIOD, FLATTEN, MANUAL, BROKER_IMPORT, KILL_SWITCH, EXPIRED_NO_FILL, UNKNOWN. Derived from the last exit order's origin and the nearest preceding strategy run/signal (heuristic: the engine has no direct order-to-run link). |
| exec.fill_count | count | |
| exec.slippage_bps_entry / exit | bps | Quantity-weighted adverse slippage of fills vs the order's `reference_price` (positive = worse than reference). |
| exec.entry_order_type / exec.entry_origin | text | MKT/LMT..., STRATEGY/MANUAL/... |
| sizing.binding_constraint / target_quantity / approved_quantity / resize_ratio | text / qty / qty / ratio | From `PositionSizingDecision` of the entry order, when it exists. |
| time.entry_bucket / time.exit_bucket / time.entry_weekday | text | 30-minute bucket and weekday in the market's zone (XNSE -> Asia/Kolkata, XNYS -> America/New_York). |
| context.prior_closed_episodes / consecutive_losses_before | count | Same instance and currency, strictly before this entry. |
| flags | list | `data_quality_flags` at computation time. |

## Stock only

| Key | Unit | Definition |
|---|---|---|
| entry.close, entry.<indicator>, entry.prev.<indicator> | price / value | Bar close and every numeric indicator at the entry decision (and the previous bar's values). |
| exit.<indicator> | value | Indicators at the exit decision. |
| path.resolution | text | Always `BAR_HIGH_LOW`: excursions use bar highs and lows only. |
| path.bars_used | count | |
| path.mae_bps / mfe_bps | bps | Maximum adverse / favourable excursion vs average entry price. |
| path.time_to_mfe_seconds | s | |
| path.capture_ratio | ratio | Realised gross return bps / MFE bps (closed episodes). |
| path.giveback_bps | bps | MFE minus realised gross return. |
| path.whipsaw_count | count | Number of bar closes crossing the entry price. |
| regime.realized_vol_20 / vol_pctile | stdev / 0-100 | 20-bar std of log returns at entry and its percentile over up to ~260 bars (needs >= 60 bars). |

Delayed bars (`market_data_type=DELAYED`) are shifted +15 minutes to wall clock before matching to the trade window and the episode gets `PAPER_DELAYED_DATA`.

## Option only

All `opt.*` values come from the entry `OptionSelectionSnapshot` (the selected ranking row); missing inputs are simply absent.

| Key | Definition |
|---|---|
| opt.snapshot_linked | True if the snapshot id was found in the entry run's context; False if matched by time. |
| opt.selected_strategy, opt.expiration, opt.structure_legs | |
| opt.entry_cash | Net cash of the opening fills (positive = credit). |
| opt.entry_fill_spread_seconds | Time between first and last leg's first fill (staging delay proxy). |
| opt.entry_score, opt.rps_threshold, margin.rps | RPS at entry, its threshold, and the margin (e.g. 61 vs 60 = +1). |
| opt.tqs, margin.tqs, opt.tqs_coverage, opt.tqs_unmeasured_count, opt.tqs_unmeasured | TQS, margin to `rps_tqs_threshold`, share of weight measured, factors left out. |
| opt.pop, opt.net_ev, opt.max_profit, opt.max_loss | Predicted at entry. |
| opt.breach_up, opt.breach_down | Predicted breach probabilities. |
| opt.iv_score, opt.iv_rank, opt.iv_percentile, margin.iv_score | IV inputs and margin to `rps_iv_score_threshold`. |
| opt.decision_band, opt.size_multiplier | TRADE_FULL / TRADE_HALF / WATCH and its size multiplier (the engine computes but does not enforce them). |
| opt.realized_fraction_of_max_profit / max_loss | net_pnl / max_profit (resp. |max_loss|), closed episodes. |
| opt.credit_capture | gross_pnl / entry credit. |
| opt.round_trip_cost_over_credit | commissions / entry credit. |

Not in v1 (needs data that is not stored historically): option MAE/MFE, live IV vs entry IV, hedge-fill ratio, bid/ask spread at entry. Episodes carry `OPTION_PATH_UNAVAILABLE` until live snapshots exist (Phase 4).

## Data-quality flags set in Phase 2

PAPER_DELAYED_DATA, ATTRIBUTION_DRIFT_POSSIBLE (an order was RESIZED by risk), MISSING_REMOTE_FEATURES, OPTION_RISK_BYPASS (state of the settings when the episode was first built; flags are never dropped later), RECONCILIATION_BREAK_DURING_TRADE, MANUAL_INTERVENTION, PARTIAL_FILLS, OPTION_PATH_UNAVAILABLE, OPTION_SNAPSHOT_UNLINKED, MIXED_CURRENCY, and from the expiry sweeper: PNL_ESTIMATED_AT_EXPIRY, EXPIRY_SETTLEMENT_UNKNOWN, EXPIRED_BROKER_STILL_HOLDS.
