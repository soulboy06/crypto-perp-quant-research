"""Linear isolated-position formulas. Caller must supply evidenced margin tiers."""
from dataclasses import dataclass
from decimal import Decimal
from .config import decimal_value as D, require

ZERO = Decimal(0)


@dataclass(frozen=True)
class Tier:
    floor: Decimal
    cap: Decimal
    maintenance_rate: Decimal
    cumulative_amount: Decimal
    maximum_venue_leverage: Decimal

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, D(getattr(self, name)))


def validate_tiers(tiers):
    require(bool(tiers), "Margin schedule missing; no default tiers")
    previous = None
    for tier in tiers:
        floor, cap, rate, cum, leverage = (D(getattr(tier, name)) for name in tier.__dataclass_fields__)
        require(0 <= floor < cap and 0 < rate < 1 and cum >= 0 and leverage >= 1, "Invalid margin tier")
        if previous is None:
            require(floor == 0 and cum == 0, "First tier must start at zero without deduction")
        else:
            require(floor == previous.cap and rate >= previous.maintenance_rate, "Noncontiguous margin schedule")
            expected = previous.cumulative_amount + floor * (rate - previous.maintenance_rate)
            require(cum == expected, "Maintenance deductions are discontinuous")
        require(floor * rate - cum >= 0, "Negative maintenance requirement")
        previous = tier
    return tiers


def tier_for(notional, tiers):
    notional = D(notional)
    require(notional >= 0, "Negative notional")
    validate_tiers(tiers)
    for index, tier in enumerate(tiers):
        if tier.floor <= notional < tier.cap or (index == len(tiers)-1 and notional == tier.cap):
            return tier
    raise ValueError("Position exceeds supplied margin-tier coverage")


def initial_margin(quantity, reference_price, leverage, tiers):
    quantity, price, leverage = D(quantity), D(reference_price), D(leverage)
    require(quantity != 0 and price > 0 and 1 <= leverage <= 10, "Invalid initial margin input")
    notional = abs(quantity) * price
    tier = tier_for(notional, tiers)
    require(leverage <= tier.maximum_venue_leverage, "Leverage exceeds supplied tier")
    return notional / leverage


def isolated_state(quantity, entry_price, isolated_wallet, mark_price, tiers):
    q, entry, wallet, mark = map(D, (quantity, entry_price, isolated_wallet, mark_price))
    require(q != 0 and entry > 0 and mark > 0, "Invalid isolated position input")
    notional = abs(q) * mark
    tier = tier_for(notional, tiers)
    maintenance = notional * tier.maintenance_rate - tier.cumulative_amount
    unrealized = q * (mark - entry)
    balance = wallet + unrealized
    return {"notional": notional, "unrealized_pnl": unrealized, "margin_balance": balance,
            "maintenance_margin": maintenance, "margin_surplus": balance - maintenance,
            "liquidation_triggered": balance <= maintenance,
            "margin_ratio": maintenance / balance if balance > 0 else None}


def liquidation_price(quantity, entry_price, isolated_wallet, tiers):
    q, entry, wallet = map(D, (quantity, entry_price, isolated_wallet))
    require(q != 0 and entry > 0 and wallet >= 0, "Invalid liquidation-price input")
    validate_tiers(tiers)
    for index, tier in enumerate(tiers):
        price = (q * entry - wallet - tier.cumulative_amount) / (q - abs(q) * tier.maintenance_rate)
        if price <= 0:
            continue
        notional = abs(q) * price
        if tier.floor <= notional < tier.cap or (index == len(tiers)-1 and notional == tier.cap):
            return price
    if q > 0 and wallet >= q * entry:
        return None  # No positive-price threshold under the supplied linear-price model.
    raise ValueError("Liquidation threshold outside supplied tier coverage")


def funding_wallet_delta(quantity, settlement_mark, funding_rate):
    q, mark, rate = map(D, (quantity, settlement_mark, funding_rate))
    require(mark > 0 and abs(rate) < 1, "Invalid funding input; exact/assumed mark must be explicit")
    return -q * mark * rate


def liquidation_fee(executed_quantity, executed_price, explicit_fee_rate):
    q, price, rate = map(D, (executed_quantity, executed_price, explicit_fee_rate))
    require(price > 0 and 0 <= rate < 1, "Invalid liquidation fee inputs")
    return abs(q) * price * rate
