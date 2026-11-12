"""Seeded option templates adapted from Aryan_Options.

The runtime definitions deliberately retain exact leg roles and expiry groups.
Contract discovery and strategy selection happen at entry time; persisted IBKR
instruments remain the only instruments allowed to reach execution.
"""

OPTION_TEMPLATES = {
    "CASH_SECURED_PUT": {
        "name": "Cash-Secured Put",
        "description": "Sell a put while reserving cash for assignment.",
        "legs": [
            {"role": "short_put", "side": "SELL", "right": "P", "offset": -1, "expiry_group": "main"},
        ],
    },
    "PUT_CREDIT_SPREAD": {
        "name": "Put Credit Spread",
        "description": "Buy a lower-strike put and sell a higher-strike put for a net credit.",
        "legs": [
            {"role": "long_put", "side": "BUY", "right": "P", "offset": -2, "expiry_group": "main"},
            {"role": "short_put", "side": "SELL", "right": "P", "offset": -1, "expiry_group": "main"},
        ],
    },
    "CALL_CREDIT_SPREAD": {
        "name": "Call Credit Spread",
        "description": "Sell a lower-strike call and buy a higher-strike call for a net credit.",
        "legs": [
            {"role": "short_call", "side": "SELL", "right": "C", "offset": 1, "expiry_group": "main"},
            {"role": "long_call", "side": "BUY", "right": "C", "offset": 2, "expiry_group": "main"},
        ],
    },
    "IRON_CONDOR": {
        "name": "Iron Condor",
        "description": "Defined-risk put and call credit spreads around the underlying.",
        # Shorts sit 2 strikes from ATM on each side, not 1. At a typical 20+
        # DTE expected move of several hundred points, a 1-strike corridor made
        # the "stay between both shorts" profit zone so narrow that POP landed
        # in the 10-25% range -- well under this family's own 45% GATE_POP
        # floor, so the selector could in practice never pick an iron condor.
        # Longs stay 1 strike beyond the shorts.
        "legs": [
            {"role": "long_put", "side": "BUY", "right": "P", "offset": -3, "expiry_group": "main"},
            {"role": "short_put", "side": "SELL", "right": "P", "offset": -2, "expiry_group": "main"},
            {"role": "short_call", "side": "SELL", "right": "C", "offset": 2, "expiry_group": "main"},
            {"role": "long_call", "side": "BUY", "right": "C", "offset": 3, "expiry_group": "main"},
        ],
    },
    "IRON_BUTTERFLY": {
        "name": "Iron Butterfly",
        "description": "Short the same centre-strike call and put with defined-risk wings.",
        # Both shorts sit on the ATM strike by definition -- that is what makes
        # it a butterfly rather than a condor, and why its POP is inherently
        # lower (a narrower profit zone is the trade it makes for a larger net
        # credit). Only the protective wings widen, to +/-2, which does not move
        # POP but reduces an otherwise extreme max-profit:max-loss ratio.
        "legs": [
            {"role": "long_put", "side": "BUY", "right": "P", "offset": -2, "expiry_group": "main"},
            {"role": "short_put", "side": "SELL", "right": "P", "offset": 0, "expiry_group": "main"},
            {"role": "short_call", "side": "SELL", "right": "C", "offset": 0, "expiry_group": "main"},
            {"role": "long_call", "side": "BUY", "right": "C", "offset": 2, "expiry_group": "main"},
        ],
    },
    "DELTA_PUT_CREDIT_SPREAD": {
        "name": "Delta Put Credit Spread",
        "description": "Sell a liquid target-delta put and buy a configurable lower-strike hedge.",
        "legs": [
            {"role": "short_put", "side": "SELL", "right": "P", "selector": "delta", "expiry_group": "main"},
            {"role": "long_put", "side": "BUY", "right": "P", "selector": "width_below", "expiry_group": "main"},
        ],
        "parameter_defaults": {"entry_min_dte": 20, "entry_max_dte": 45, "hold_days": 10},
    },
    "LONG_ATM_STRADDLE": {
        "name": "Long ATM Straddle",
        "description": "Buy an ATM call and put in one expiry to express long volatility.",
        "legs": [
            {"role": "long_call", "side": "BUY", "right": "C", "offset": 0, "expiry_group": "main"},
            {"role": "long_put", "side": "BUY", "right": "P", "offset": 0, "expiry_group": "main"},
        ],
    },
    "CALENDAR_CALL": {
        "name": "Calendar Call Strategy",
        "description": "Buy a farther-expiry ATM call and sell a nearer-expiry ATM call.",
        "legs": [
            {"role": "long_call", "side": "BUY", "right": "C", "offset": 0, "expiry_group": "far"},
            {"role": "short_call", "side": "SELL", "right": "C", "offset": 0, "expiry_group": "near"},
        ],
        "parameter_defaults": {"entry_min_dte": 1, "entry_max_dte": 90},
    },
}

AUTO_OPTION_STRATEGY_KEY = "RPS_OPTIONS_AUTO_SELECTOR"
# The source's `DEFAULT_RISK_STRATEGY_KEYS`. A long straddle is not a
# defined-risk credit structure and the approved selector has no family, POP
# floor or credit gate for it, so it is not offered to the automatic selector.
AUTO_CANDIDATES = (
    "PUT_CREDIT_SPREAD",
    "CALL_CREDIT_SPREAD",
    "IRON_CONDOR",
    "IRON_BUTTERFLY",
)


COMMON_DEFAULTS = {
    "direction": "MULTI_LEG",
    "lots": 1,
    "option_strike_count": 21,
    "round_trip_cost_per_lot": 40,
    "atr_period": 14,
    "atr_multiplier": 0.75,
    "rps_threshold": 60,
    "entry_min_dte": 1,
    "entry_max_dte": 45,
    "minimum_open_interest": 100,
    "minimum_volume": 1,
    "hold_days": 0,
    "exit_dte": 0,
    "one_entry_per_expiry": True,
    # The source's entry window (`trading/rps.py`): a decision is refused until
    # the session has been open for `rps_entry_delay_minutes`. At 1d this only
    # ever passes, but it is what lets an intraday timeframe sample the window
    # the way the source's replay does.
    "rps_market_open_time": "09:15",
    "rps_entry_delay_minutes": 0,
    # The source localises its session index to Asia/Kolkata; nothing here
    # records IBKR's timeZoneId, so the zone is configurable with that default.
    "rps_market_timezone": "Asia/Kolkata",
    "target_delta": -0.20,
    "strike_width": 100,
    # Approved range/IV/breach entry gate.
    "rps_iv_breach_enabled": True,
    "rps_iv_score_threshold": 60,
    "rps_max_upper_breach_probability": 0.50,
    "rps_max_lower_breach_probability": 0.50,
    "rps_breach_simulation_paths": 10000,
    "rps_pop_risk_free_rate": 0.068,
    "rps_pop_dividend_yield": 0.012,
    # Pre-selection filters and the 14-factor selector.
    "rps_min_pop": 0,
    "rps_require_positive_ev": False,
    "rps_max_entry_loss": 0,
    "rps_tqs_threshold": 60,
    "rps_tqs_min_net_pnl": 0,
    # Soft-exit policy. Positions are held to expiry; these three bank an edge
    # that already exists, they never close on a range or risk reading.
    "dynamic_exit_enabled": True,
    "dynamic_exit_soft_max_giveback": 0.25,
    "dynamic_exit_soft_giveback_probability": 0.40,
    "dynamic_exit_soft_profit_capture": 0.70,
    "dynamic_exit_soft_volume_increase": 0.50,
    "dynamic_exit_soft_iv_increase": 0.20,
    "dynamic_exit_soft_min_ev_improvement": 0.05,
    "dynamic_exit_soft_remaining_profit": 0.30,
    "dynamic_exit_require_volume_and_iv": True,
}


def option_parameter_schema():
    return {
        "type": "object",
        "required": [
            "direction",
            "lots", "atr_period", "atr_multiplier", "rps_threshold",
            "entry_min_dte", "entry_max_dte", "minimum_open_interest",
        ],
        "properties": {
            "direction": {"type": "string", "const": "MULTI_LEG", "default": "MULTI_LEG"},
            "option_strike_count": {"type": "integer", "minimum": 5, "maximum": 21, "default": 21},
            "option_trading_class": {"type": "string", "default": ""},
            "round_trip_cost_per_lot": {"type": "number", "minimum": 0, "default": 40},
            "lots": {"type": "integer", "minimum": 1, "default": 1, "title": "Lots"},
            "atr_period": {"type": "integer", "minimum": 2, "maximum": 52, "default": 14, "title": "Daily Wilder ATR period"},
            "atr_multiplier": {"type": "number", "minimum": 0.75, "maximum": 0.75, "default": 0.75, "title": "ATR multiplier"},
            "rps_threshold": {"type": "number", "minimum": 0, "maximum": 100, "default": 60, "title": "RPS entry floor"},
            "entry_min_dte": {"type": "integer", "minimum": 1, "maximum": 90, "default": 1},
            "entry_max_dte": {"type": "integer", "minimum": 1, "maximum": 90, "default": 45},
            "minimum_open_interest": {"type": "integer", "minimum": 0, "default": 100},
            "minimum_volume": {"type": "integer", "minimum": 0, "default": 1},
            "hold_days": {"type": "integer", "minimum": 0, "default": 0},
            "exit_dte": {"type": "integer", "minimum": 0, "default": 0},
            "one_entry_per_expiry": {"type": "boolean", "default": True},
            "rps_market_open_time": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$", "default": "09:15", "title": "Session open"},
            "rps_entry_delay_minutes": {"type": "integer", "minimum": 0, "maximum": 375, "default": 0, "title": "Entry delay after open"},
            "rps_market_timezone": {"type": "string", "default": "Asia/Kolkata", "title": "Exchange timezone"},
            "target_delta": {"type": "number", "minimum": -0.95, "maximum": -0.01, "default": -0.20},
            "strike_width": {"type": "number", "exclusiveMinimum": 0, "default": 100},
            "rps_iv_breach_enabled": {"type": "boolean", "default": True},
            "rps_iv_score_threshold": {"type": "number", "minimum": 0, "maximum": 100, "default": 60},
            "rps_max_upper_breach_probability": {"type": "number", "exclusiveMinimum": 0, "maximum": 100, "default": 0.50},
            "rps_max_lower_breach_probability": {"type": "number", "exclusiveMinimum": 0, "maximum": 100, "default": 0.50},
            "rps_max_two_sided_breach_probability": {"type": ["number", "null"], "default": None},
            "rps_breach_simulation_paths": {"type": "integer", "minimum": 1000, "maximum": 200000, "default": 10000},
            "rps_pop_risk_free_rate": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.068},
            "rps_pop_dividend_yield": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.012},
            "rps_min_pop": {"type": "number", "minimum": 0, "maximum": 100, "default": 0},
            "rps_require_positive_ev": {"type": "boolean", "default": False},
            "rps_max_entry_loss": {"type": "number", "minimum": 0, "default": 0},
            "rps_tqs_threshold": {"type": "number", "minimum": 0, "maximum": 100, "default": 60},
            "rps_tqs_min_net_pnl": {"type": "number", "default": 0},
            "dynamic_exit_enabled": {"type": "boolean", "default": True},
            "dynamic_exit_soft_max_giveback": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.25},
            "dynamic_exit_soft_giveback_probability": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.40},
            "dynamic_exit_soft_profit_capture": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.70},
            "dynamic_exit_soft_volume_increase": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.50},
            "dynamic_exit_soft_iv_increase": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.20},
            "dynamic_exit_soft_min_ev_improvement": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.05},
            "dynamic_exit_soft_remaining_profit": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.30},
            "dynamic_exit_require_volume_and_iv": {"type": "boolean", "default": True},
            "dhan_security_id": {"type": "string", "default": ""},
            "dhan_segment": {"type": "string", "default": ""},
        },
        "additionalProperties": True,
    }
