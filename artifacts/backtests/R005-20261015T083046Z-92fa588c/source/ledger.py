"""Simulation-only shared cash ledger. Explicit rules; no orders sent to a venue."""
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
from .config import decimal_value as D, require, validate
from .margin import initial_margin, isolated_state, funding_wallet_delta, validate_tiers
from .risk import EntryRequest, preview

ZERO = Decimal(0)
DAY_US = 86_400_000_000


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InstrumentRules:
    step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal
    tiers: tuple
    valid_from_us: int
    valid_to_us: int
    evidence_id: str

    def __post_init__(self):
        for name in ("step", "min_quantity", "max_quantity", "min_notional"):
            object.__setattr__(self, name, D(getattr(self, name)))
        object.__setattr__(self, "tiers", tuple(self.tiers))
        require(0 < self.step <= self.min_quantity <= self.max_quantity and self.min_notional > 0,
                "Invalid supplied order filters")
        require(type(self.valid_from_us) is int and type(self.valid_to_us) is int
                and 0 <= self.valid_from_us < self.valid_to_us and bool(self.evidence_id), "Rule provenance/validity missing")
        validate_tiers(self.tiers)

    def at(self, stamp):
        require(self.valid_from_us <= stamp < self.valid_to_us, "No rule schedule valid at this event")
        return self


class Ledger:
    """One strategy account. Persistent protection state survives verified journal replay.

    Funding debit is available cash first, then the affected isolated wallet; credit
    is cash. This explicit research convention is NOT verified for historical dates.
    Execution prices already include caller-modelled slippage; fees are charged here.
    """

    def __init__(self, config, rules, *, funding_policy, leverage_cap="1", cost="base", journal=None):
        self.config = deepcopy(validate(config))
        require(funding_policy == "available_first_credit_cash_research_v1", "Explicit supported funding policy required")
        require(set(rules) == set(config["market"]["symbols"]), "Both instruments need supplied rules")
        require(all(isinstance(r, InstrumentRules) for r in rules.values()), "Invalid rules")
        self.rules = dict(rules)
        self.cap = D(leverage_cap)
        require(self.cap in [D(x) for x in config["risk"]["leverage_scenarios"]] and self.cap <= 10, "Invalid leverage scenario")
        require(cost in ("base", "stress"), "Unknown cost scenario")
        self.cost = cost
        self.fee = D(config["costs"][cost]["taker_fee_rate"])
        self.friction = self.fee + D(config["costs"][cost]["adverse_price_rate"])
        from datetime import datetime
        self.start = int(datetime.fromisoformat(config["data"]["development_start"].replace("Z", "+00:00")).timestamp()) * 1_000_000
        self.end = int(datetime.fromisoformat(config["data"]["development_end"].replace("Z", "+00:00")).timestamp()) * 1_000_000
        initial = D(config["account"]["initial_usd"])
        self._state = {"cash": initial, "positions": {}, "orders": {}, "realized_pnl": ZERO,
                       "fees": ZERO, "funding": ZERO, "last_time_us": self.start, "day": self.start // DAY_US,
                       "day_start_equity": initial, "peak_equity": initial, "daily_paused": False,
                       "halt_reasons": [], "sequence": 0}
        self._seen = {}
        self._chain = "0" * 64
        self._connection = None
        self._closed = False
        self.binding = {"schema": "LEDGER-001-v1", "config_sha256": digest(config),
                        "rules_sha256": digest({s: asdict(r) for s, r in self.rules.items()}),
                        "funding_policy": funding_policy, "leverage_cap": str(self.cap), "cost": cost,
                        "historical_rule_verification": "caller_responsibility_not_established_by_ledger"}
        if journal is not None:
            self._open_journal(journal)

    def _open_journal(self, path):
        connection = sqlite3.connect(Path(path), isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE IF NOT EXISTS metadata(binding TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, state_hash TEXT NOT NULL, chain_hash TEXT NOT NULL)")
            rows = connection.execute("SELECT binding FROM metadata").fetchall()
            if not rows:
                require(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0, "Orphan journal events")
                connection.execute("INSERT INTO metadata VALUES (?)", (encoded(self.binding),))
            else:
                require(len(rows) == 1 and rows[0][0] == encoded(self.binding), "Journal configuration/rules differ")
            for seq, event_id, payload, state_hash, chain in connection.execute("SELECT * FROM events ORDER BY seq"):
                event = json.loads(payload)
                require(event["id"] == event_id and seq == self._state["sequence"] + 1, "Journal sequence/identity invalid")
                self.apply(event)
                require(digest(self._state) == state_hash and self._chain == chain, "Journal replay/hash mismatch")
            self._connection = connection
        except Exception:
            connection.close()
            raise

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._closed = True

    def snapshot(self):
        return json.loads(encoded(self._state))

    @staticmethod
    def equity_of(state):
        return (state["cash"] + sum((o["reserved"] for o in state["orders"].values()), ZERO)
                + sum((p["wallet"] + p["quantity"] * (p["mark"] - p["entry"])
                       for p in state["positions"].values()), ZERO))

    @property
    def equity(self):
        return self.equity_of(self._state)

    def _rules(self, symbol, stamp):
        require(symbol in self.rules, "Symbol outside universe")
        return self.rules[symbol].at(stamp)

    def _fresh(self, state, stamp):
        require(all(p["mark_time_us"] == stamp for p in state["positions"].values()),
                "Fresh mark snapshot for every open position required before increasing exposure")

    def _risk_values(self, state):
        gross, open_risk, pending, pending_risk = ZERO, ZERO, ZERO, ZERO
        buffer = D(self.config["risk"]["gap_buffer_rate"]) + D(self.config["risk"]["funding_buffer_rate"])
        for p in state["positions"].values():
            q, mark = p["quantity"], p["mark"]
            gross += abs(q) * mark
            distance = max(ZERO, mark-p["stop"] if q > 0 else p["stop"]-mark)
            open_risk += abs(q) * (distance + mark * (self.friction + buffer))  # Entry friction is already in equity.
        for o in state["orders"].values():
            notional = o["remaining"] * o["reference"]
            pending += notional
            pending_risk += o["remaining"] * abs(o["reference"]-o["stop"]) + notional*(2*self.friction+buffer)
        return gross, open_risk, pending, pending_risk

    def _reserve(self, state, event):
        stamp, symbol = event["time_us"], event["symbol"]
        rules = self._rules(symbol, stamp)
        require(not state["halt_reasons"] and not state["daily_paused"], "Account entry protection active")
        require(symbol not in state["positions"] and all(o["symbol"] != symbol for o in state["orders"].values()),
                "Close/cancel the symbol before opening or reversing; no pyramiding")
        self._fresh(state, stamp)
        price, stop = D(event["reference_price"]), D(event["stop_price"])
        equity = self.equity_of(state)
        gross, risk, pending, pending_risk = self._risk_values(state)
        # preview subtracts pending reserves itself; do not subtract them twice.
        before_pending = state["cash"] + sum((o["reserved"] for o in state["orders"].values()), ZERO)
        request = EntryRequest(equity, min(before_pending, equity), price, stop, rules.step, rules.min_quantity,
                               rules.min_notional, side=event["side"], existing_gross=gross,
                               pending_gross=pending, existing_stop_risk=risk, pending_stop_risk=pending_risk)
        result = preview(self.config, request, self.cap, self.cost)
        require(result["status"] == "preview_only", "Entry rejected: " + result.get("reason", "unknown"))
        quantity = D(event.get("quantity", str(result["quantity"])))
        require(rules.min_quantity <= quantity <= min(rules.max_quantity, result["quantity"])
                and quantity % rules.step == 0 and quantity*price >= rules.min_notional, "Quantity/filter/risk budget violation")
        initial_margin(quantity, price, D(self.config["risk"]["venue_leverage"]), rules.tiers)
        reserve_rate = (1 / D(self.config["risk"]["venue_leverage"]) + 2*self.friction
                        + D(self.config["risk"]["funding_buffer_rate"]))
        reserved = quantity * price * reserve_rate
        require(reserved <= state["cash"], "Insufficient shared cash")
        order_id = event["order_id"]
        require(isinstance(order_id, str) and order_id and order_id not in state.setdefault("order_ids", []), "Duplicate/invalid order id")
        state["order_ids"].append(order_id)
        state["cash"] -= reserved
        state["orders"][order_id] = {"symbol": symbol, "remaining": quantity, "reference": price,
                                     "stop": stop, "sign": 1 if event["side"] == "long" else -1, "reserved": reserved}

    def _fill(self, state, event):
        stamp = event["time_us"]
        require(not state["halt_reasons"] and not state["daily_paused"], "Cannot fill a new entry after protection")
        self._fresh(state, stamp)
        order = state["orders"][event["order_id"]]
        symbol = order["symbol"]
        rules = self._rules(symbol, stamp)
        q, price, mark = map(D, (event["quantity"], event["price"], event["mark_price"]))
        require(0 < q <= order["remaining"] and q % rules.step == 0 and price > 0 and mark > 0, "Invalid simulated fill")
        require(order["stop"] < min(price, mark) if order["sign"] > 0 else order["stop"] > max(price, mark), "Entry stop already crossed")
        margin = initial_margin(q, price, D(self.config["risk"]["venue_leverage"]), rules.tiers)
        fee = q * price * self.fee
        released = order["reserved"] * q / order["remaining"]
        require(margin + fee <= released, "Fill exceeds reserved collateral; reprice/cancel before simulated execution")
        state["cash"] += released - margin - fee
        state["fees"] += fee
        if symbol not in state["positions"]:
            state["positions"][symbol] = {"quantity": ZERO, "entry": price, "wallet": ZERO, "mark": mark,
                                           "mark_time_us": stamp, "stop": order["stop"]}
        p = state["positions"][symbol]
        require(p["quantity"] * order["sign"] >= 0, "Reversal requires a separate flat state")
        p["entry"] = (abs(p["quantity"])*p["entry"] + q*price) / (abs(p["quantity"])+q)
        p["quantity"] += order["sign"] * q
        p["wallet"] += margin
        p["mark"], p["mark_time_us"] = mark, stamp
        initial_margin(p["quantity"], p["entry"], D(self.config["risk"]["venue_leverage"]), rules.tiers)
        order["remaining"] -= q
        order["reserved"] -= released
        if order["remaining"] == 0:
            del state["orders"][event["order_id"]]
        gross, risk, pending, pending_risk = self._risk_values(state)
        equity = self.equity_of(state)
        projected = equity - pending * self.friction
        require(projected > 0 and gross + pending <= self.cap * projected, "Fill exceeds shared gross leverage cap")
        require(abs(p["quantity"])*mark <= D(self.config["risk"]["max_symbol_leverage"])*projected, "Symbol leverage cap")
        require(risk + pending_risk <= equity * D(self.config["risk"]["total_open_risk_fraction"]), "Fill exceeds shared stop risk")
        symbol_state = {"positions": {symbol: p}, "orders": {k: v for k, v in state["orders"].items() if v["symbol"] == symbol}}
        _, symbol_risk, symbol_pending, symbol_pending_risk = self._risk_values(symbol_state)
        require(symbol_risk + symbol_pending_risk <= equity * D(self.config["risk"]["per_trade_risk_fraction"]), "Fill exceeds per-trade stop risk")
        require(abs(p["quantity"])*mark + symbol_pending <= D(self.config["risk"]["max_symbol_leverage"])*projected, "Pending symbol leverage cap")
        require(state["cash"] >= equity * D(self.config["risk"]["free_collateral_reserve_fraction"]), "Fill consumes cash reserve")

    def _cancel(self, state, order_id):
        order = state["orders"].pop(order_id)
        state["cash"] += order["reserved"]

    def _exit(self, state, event):
        symbol, stamp = event["symbol"], event["time_us"]
        rules = self._rules(symbol, stamp)
        p = state["positions"][symbol]
        require(all(o["symbol"] != symbol for o in state["orders"].values()), "Cancel remaining entry before reducing")
        q, price, mark = map(D, (event["quantity"], event["price"], event["mark_price"]))
        require(0 < q <= abs(p["quantity"]) and q % rules.step == 0 and price > 0 and mark > 0, "Invalid reduce-only fill")
        released = p["wallet"] * q / abs(p["quantity"])
        realized = (1 if p["quantity"] > 0 else -1) * q * (price-p["entry"])
        fee = q * price * self.fee
        state["cash"] += released + realized - fee
        state["realized_pnl"] += realized
        state["fees"] += fee
        p["wallet"] -= released
        p["quantity"] -= (1 if p["quantity"] > 0 else -1) * q
        p["mark"], p["mark_time_us"] = mark, stamp
        if p["quantity"] == 0:
            del state["positions"][symbol]

    def _funding(self, state, event):
        symbol, stamp = event["symbol"], event["time_us"]
        self._rules(symbol, stamp)
        price, rate = D(event["mark_price"]), D(event["rate"])
        require(type(event["exact_mark"]) is bool and price > 0 and abs(rate) < 1, "Invalid funding valuation")
        if not event["exact_mark"]:
            source = event["source_available_us"]
            require(type(source) is int and 0 <= stamp-source <= 60_000_000, "Funding proxy stale or from future")
        key = f"{symbol}:{stamp}"
        require(key not in state.setdefault("funding_events", []), "Duplicate economic funding event")
        state["funding_events"].append(key)
        if symbol not in state["positions"]:
            return
        p = state["positions"][symbol]
        delta = funding_wallet_delta(p["quantity"], price, rate)
        if delta >= 0:
            state["cash"] += delta
        else:
            debit = min(max(ZERO, state["cash"]), -delta)
            state["cash"] -= debit
            p["wallet"] += delta + debit
        state["funding"] += delta
        p["mark"], p["mark_time_us"] = price, stamp

    def _protect(self, state, stamp):
        equity = self.equity_of(state)
        state["peak_equity"] = max(state["peak_equity"], equity)
        reasons = state["halt_reasons"]
        gross = sum((abs(p["quantity"])*p["mark"] for p in state["positions"].values()), ZERO)
        state["exposure_reduction_required"] = gross > 0 and (equity <= 0 or gross > self.cap*equity)
        if equity <= D(self.config["account"]["stop_equity_usd"]) and "capital_stop" not in reasons:
            reasons.append("capital_stop")
        if equity <= state["peak_equity"]*(1-D(self.config["risk"]["drawdown_halt_fraction"])) and "drawdown_halt" not in reasons:
            reasons.append("drawdown_halt")
        if equity <= state["day_start_equity"]*(1-D(self.config["risk"]["daily_loss_pause_fraction"])):
            state["daily_paused"] = True
        for symbol, p in state["positions"].items():
            condition = isolated_state(p["quantity"], p["entry"], p["wallet"], p["mark"], self._rules(symbol, stamp).tiers)
            if condition["liquidation_triggered"] and f"liquidation_required:{symbol}" not in reasons:
                reasons.append(f"liquidation_required:{symbol}")
        if state["cash"] < 0 and "cash_deficit" not in reasons:
            reasons.append("cash_deficit")
        if reasons or state["daily_paused"] or state["exposure_reduction_required"]:
            for order_id in list(state["orders"]):
                self._cancel(state, order_id)

    def apply(self, event):
        # JSON-compatible payloads with decimal strings are the durable public API.
        require(not self._closed, "Ledger has been closed; reopen its journal")
        payload = encoded(event)
        event = json.loads(payload)
        require(isinstance(event, dict) and isinstance(event.get("id"), str) and event["id"], "Event id required")
        if event["id"] in self._seen:
            require(self._seen[event["id"]] == payload, "Conflicting duplicate event id")
            return self.snapshot()
        stamp = event["time_us"]
        require(type(stamp) is int and self.start <= stamp < self.end and stamp >= self._state["last_time_us"], "Invalid/non-monotonic development event time")
        with localcontext() as context:
            context.prec = 40
            state = deepcopy(self._state)
            kind = event["type"]
            day = stamp // DAY_US
            require(day == state["day"] or (day == state["day"]+1 and kind == "mark" and stamp % DAY_US == 0),
                    "UTC day boundary requires a midnight all-position mark event; no skipped days")
            if kind == "mark":
                require(set(event["prices"]) == set(state["positions"]), "Mark snapshot must cover exactly the open symbols")
                for symbol, price in event["prices"].items():
                    require(D(price) > 0, "Nonpositive mark")
                    self._rules(symbol, stamp)
                    state["positions"][symbol].update(mark=D(price), mark_time_us=stamp)
            if day != state["day"]:
                state.update(day=day, day_start_equity=self.equity_of(state), daily_paused=False)
            if kind == "reserve":
                self._reserve(state, event)
            elif kind == "fill":
                self._fill(state, event)
            elif kind == "cancel":
                self._cancel(state, event["order_id"])
            elif kind == "exit":
                self._exit(state, event)
            elif kind == "funding":
                self._funding(state, event)
            else:
                require(kind == "mark", "Unsupported ledger event")
            state["last_time_us"] = stamp
            self._protect(state, stamp)
            state["sequence"] += 1
            unrealized = sum((p["quantity"]*(p["mark"]-p["entry"]) for p in state["positions"].values()), ZERO)
            expected = D(self.config["account"]["initial_usd"]) + state["realized_pnl"] + state["funding"] - state["fees"] + unrealized
            require(abs(self.equity_of(state)-expected) <= D("1e-25"), "Wallet conservation failure")
            chain = digest({"previous": self._chain, "event": event, "state": digest(state)})
            if self._connection is not None:
                try:
                    self._connection.execute("BEGIN IMMEDIATE")
                    self._connection.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                                             (state["sequence"], event["id"], payload, digest(state), chain))
                    self._connection.execute("COMMIT")
                except Exception:
                    self._connection.execute("ROLLBACK")
                    raise
            self._state, self._chain = state, chain
            self._seen[event["id"]] = payload
        return self.snapshot()
