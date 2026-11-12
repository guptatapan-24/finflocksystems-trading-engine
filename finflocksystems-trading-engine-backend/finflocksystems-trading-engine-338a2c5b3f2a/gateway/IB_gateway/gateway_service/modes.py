VALID_IBKR_MODES = {"paper": 4002, "live": 4001}


def normalize_trading_mode(value):
    mode = str(value or "").strip().lower()
    if mode not in VALID_IBKR_MODES:
        raise ValueError("IBC_TRADING_MODE must be exactly paper or live")
    return mode


def tws_port_for_mode(value):
    return VALID_IBKR_MODES[normalize_trading_mode(value)]
