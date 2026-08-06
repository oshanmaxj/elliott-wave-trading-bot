from decimal import Decimal, ROUND_DOWN


def floor_quantity_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_price_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    return floor_quantity_to_step(value, tick)


def _filter(info, kind):
    return next(
        (x for x in info.get("filters", []) if x.get("filterType") == kind), None
    )


def validate_notional(price: Decimal, quantity: Decimal, info: dict) -> list[str]:
    f = _filter(info, "NOTIONAL") or _filter(info, "MIN_NOTIONAL")
    n = price * quantity
    reasons = []
    if f and n < Decimal(f.get("minNotional", "0")):
        reasons.append("notional_below_minimum")
    if f and f.get("maxNotional") and n > Decimal(f["maxNotional"]):
        reasons.append("notional_above_maximum")
    return reasons


def validate_symbol_tradeability(info: dict, order_type="MARKET") -> list[str]:
    reasons = []
    if info.get("status") != "TRADING":
        reasons.append("symbol_not_trading")
    if order_type not in info.get("orderTypes", []):
        reasons.append("order_type_not_allowed")
    return reasons


def quantity_limits(info, market=True):
    f = _filter(info, "MARKET_LOT_SIZE") if market else None
    f = f or _filter(info, "LOT_SIZE") or {}
    return (
        Decimal(f.get("minQty", "0")),
        Decimal(f.get("maxQty", "1E99")),
        Decimal(f.get("stepSize", "1")),
    )
