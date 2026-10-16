"""Pure entry-size preview. It neither checks liquidation nor executes orders."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from .config import decimal_value as D, require

ZERO = Decimal(0)


@dataclass(frozen=True)
class EntryRequest:
    equity: Decimal
    free_collateral: Decimal
    reference_price: Decimal
    stop_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    side: str = "long"
    existing_gross: Decimal = ZERO
    existing_symbol_gross: Decimal = ZERO
    pending_gross: Decimal = ZERO
    pending_symbol_gross: Decimal = ZERO
    existing_stop_risk: Decimal = ZERO
    pending_stop_risk: Decimal = ZERO
    halted: bool = False


def preview(config, request, leverage_cap=None, cost="base"):
    risk, costs = config["risk"], config["costs"][cost]
    fields = {name: D(getattr(request, name)) for name in request.__dataclass_fields__
              if name not in ("side", "halted")}
    require(all(value >= 0 for value in fields.values()), "Negative monetary input")
    require(all(fields[key] > 0 for key in ("reference_price", "stop_price", "step_size")),
            "Price and step must be positive")
    require(request.side in ("long", "short"), "Invalid side")
    price, stop, equity = (fields[key] for key in ("reference_price", "stop_price", "equity"))
    require(stop < price if request.side == "long" else stop > price, "Stop on wrong side")
    require(fields["free_collateral"] <= equity, "Free collateral exceeds equity")
    require(fields["existing_symbol_gross"] <= fields["existing_gross"], "Invalid existing exposure")
    require(fields["pending_symbol_gross"] <= fields["pending_gross"], "Invalid pending exposure")
    limit = D(risk["baseline_leverage_cap"] if leverage_cap is None else leverage_cap)
    require(limit in [D(value) for value in risk["leverage_scenarios"]], "Unregistered leverage scenario")
    require(0 < limit <= D(risk["max_effective_leverage"]) <= 10, "Leverage limit violated")
    if request.halted or equity <= D(config["account"]["stop_equity_usd"]):
        return {"status": "rejected", "reason": "account_halted_or_capital_stop", "quantity": ZERO}
    entry_cost = D(costs["taker_fee_rate"]) + D(costs["adverse_price_rate"])
    pending_cost = fields["pending_gross"] * entry_cost
    # Absolute notional, not net exposure; account equity is reduced by entry friction.
    gross_room = (limit * (equity - pending_cost) - fields["existing_gross"]
                  - fields["pending_gross"]) / (1 + limit * entry_cost)
    symbol_limit = D(risk["max_symbol_leverage"])
    symbol_room = (symbol_limit * (equity - pending_cost) - fields["existing_symbol_gross"]
                   - fields["pending_symbol_gross"]) / (1 + symbol_limit * entry_cost)
    round_cost = 2 * entry_cost
    funding_buffer = D(risk["funding_buffer_rate"])
    risk_rate = abs(price - stop) / price + round_cost + D(risk["gap_buffer_rate"]) + funding_buffer
    risk_budget = min(equity * D(risk["per_trade_risk_fraction"]),
                      equity * D(risk["total_open_risk_fraction"])
                      - fields["existing_stop_risk"] - fields["pending_stop_risk"])
    margin_rate = 1 / D(risk["venue_leverage"]) + round_cost + funding_buffer
    reserved_pending = fields["pending_gross"] * margin_rate
    margin_room = (fields["free_collateral"] - reserved_pending
                   - equity * D(risk["free_collateral_reserve_fraction"])) / margin_rate
    rooms = {"stop_risk": risk_budget / risk_rate, "account_leverage": gross_room,
             "symbol_leverage": symbol_room, "collateral": margin_room}
    binding = min(rooms, key=rooms.get)
    maximum = max(ZERO, rooms[binding])
    quantity = (maximum / price / fields["step_size"]).to_integral_value(rounding=ROUND_FLOOR)
    quantity *= fields["step_size"]
    notional = quantity * price
    if quantity <= 0 or quantity < fields["min_qty"] or notional < fields["min_notional"]:
        return {"status": "rejected", "reason": "budget_or_minimum_order", "quantity": ZERO,
                "binding_constraint": binding}
    projected_equity = equity - pending_cost - notional * entry_cost
    require(projected_equity > 0, "Nonpositive projected equity")
    return {"status": "preview_only", "quantity": quantity, "notional": notional,
            "risk_budget": risk_budget, "projected_risk": notional * risk_rate,
            "estimated_margin_and_reserves": notional * margin_rate,
            "projected_equity_after_entry": projected_equity,
            "projected_gross_leverage": (fields["existing_gross"] + fields["pending_gross"]
                                         + notional) / projected_equity,
            "binding_constraint": binding,
            "not_checked": ["liquidation", "exchange_rules", "live_orders", "portfolio_halt_state"]}
