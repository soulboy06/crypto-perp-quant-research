"""Validate design constraints without opening any market-data partition."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tomllib


def decimal_value(value):
    if isinstance(value, (bool, float)):
        raise ValueError("Use decimal strings or integers, not floats/bools")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid decimal: {value!r}") from exc
    if not number.is_finite():
        raise ValueError("Non-finite number")
    return number


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(config):
    require(config["mode"] == "simulation", "Only simulation is supported")
    market, account, risk = (config[key] for key in ("market", "account", "risk"))
    require(market["venue"] == "binance_usdm", "Wrong venue")
    require(market["symbols"] == ["BTCUSDT", "ETHUSDT"], "Wrong first-round universe")
    require(market["margin_mode"] == "isolated", "Isolated margin is required")
    require(market["position_mode"] == "one_way", "One-way mode is required")
    require(market["auto_add_margin"] is False, "Auto-add margin is forbidden")
    require(decimal_value(account["initial_usd"]) == 100, "Initial capital must be USD 100")
    require(decimal_value(account["stop_equity_usd"]) == 50, "Capital stop must be USD 50")
    require(decimal_value(account["usd_per_usdt"]) == 1, "Only explicit parity scenario is implemented")
    require(account["fx_assumption"] == "constant_parity_research_only", "FX assumption missing")
    leverage = decimal_value(risk["max_effective_leverage"])
    require(0 < leverage <= 10, "Account leverage must not exceed 10")
    require(0 < decimal_value(risk["venue_leverage"]) <= 10, "Venue leverage exceeds 10")
    for key in ("baseline_leverage_cap", "max_symbol_leverage"):
        require(0 < decimal_value(risk[key]) <= leverage, f"Invalid {key}")
    for key in ("per_trade_risk_fraction", "total_open_risk_fraction",
                "daily_loss_pause_fraction", "drawdown_halt_fraction",
                "free_collateral_reserve_fraction"):
        require(0 < decimal_value(risk[key]) < 1, f"Invalid {key}")
    require(decimal_value(risk["per_trade_risk_fraction"]) <=
            decimal_value(risk["total_open_risk_fraction"]), "Per-trade risk exceeds total")
    require(decimal_value(risk["drawdown_halt_fraction"]) < Decimal("0.5"),
            "Research drawdown protection must be earlier than capital loss limit")
    for key in ("gap_buffer_rate", "funding_buffer_rate"):
        require(decimal_value(risk[key]) >= 0, f"Invalid {key}")
    scenarios = [decimal_value(value) for value in risk["leverage_scenarios"]]
    require(scenarios == sorted(set(scenarios)) and scenarios[0] == 1,
            "Leverage scenarios must be unique, ordered, and start at 1")
    require(all(0 < value <= leverage for value in scenarios), "Scenario exceeds leverage limit")
    for name in ("base", "stress"):
        for key in ("taker_fee_rate", "adverse_price_rate"):
            require(0 <= decimal_value(config["costs"][name][key]) < 1, f"Invalid {name} {key}")
    for key in ("taker_fee_rate", "adverse_price_rate"):
        require(decimal_value(config["costs"]["stress"][key]) >=
                decimal_value(config["costs"]["base"][key]), "Stress cost below base")
    data = config["data"]
    keys = ("warmup_start", "development_start", "development_end", "holdout_start", "holdout_end")
    dates = []
    for key in keys:
        stamp = data[key]
        require(isinstance(stamp, str) and stamp.endswith("Z"), f"UTC required: {key}")
        dates.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
    require(dates[0] < dates[1] < dates[2] <= dates[3] < dates[4], "Invalid time boundaries")
    require(data["holdout_locked"] is True, "Holdout cannot be unlocked by this command")
    require(data["mark_price_required"] is True and data["funding_required"] is True,
            "Mark-price and funding data are mandatory")
    require(data["signal_interval"] == "1h" and data["execution_interval"] == "1m",
            "First-round timeframes are fixed")
    require(type(data["decision_delay_seconds"]) is int and data["decision_delay_seconds"] >= 60,
            "Decision delay must be at least one minute")
    require(config["research"]["parameter_search"] is False, "Parameter search is disabled")
    for key in ("engine_implemented", "market_data_verified", "funding_data_verified",
                "exchange_filters_verified", "margin_rules_verified", "liquidation_model_verified"):
        require(type(config["readiness"][key]) is bool, f"Invalid readiness field: {key}")
    return config


def load(path):
    with Path(path).open("rb") as stream:
        return validate(tomllib.load(stream))


def readiness(config, project_root):
    """A setup inventory, not proof of correctness; never reads dataset contents."""
    missing = [key for key, value in config["readiness"].items() if value is not True]
    root = Path(project_root).resolve()
    for label, relative in config["artifacts"].items():
        target = (root / relative).resolve()
        require(target.is_relative_to(root), "Artifact path escapes project")
        if not target.is_file():
            missing.append(f"missing:{label}")
    return missing
