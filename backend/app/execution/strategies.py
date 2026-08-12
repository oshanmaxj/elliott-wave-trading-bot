from typing import Any


SETUP_TO_RUNTIME_STRATEGY = {
    "bullish_continuation": "bos_continuation",
    "bearish_continuation": "bos_continuation",
    "bullish_liquidity_reversal": "liquidity_sweep_reversal",
    "bearish_liquidity_reversal": "liquidity_sweep_reversal",
    "bullish_wave_3": "wave_3_continuation",
    "bearish_wave_3": "wave_3_continuation",
    "bullish_wave_5": "wave_5_continuation",
    "bearish_wave_5": "wave_5_continuation",
    "bullish_c_wave": "c_wave_reversal",
    "bearish_c_wave": "c_wave_reversal",
}


def runtime_strategy_for_setup_name(setup_strategy: str) -> str | None:
    return SETUP_TO_RUNTIME_STRATEGY.get(setup_strategy)


def originating_runtime_strategy(setup: Any) -> str | None:
    """Return the dashboard strategy that authorized a persisted setup."""
    conditions = getattr(setup, "setup_conditions_json", None) or {}
    explicit = conditions.get("originating_runtime_strategy_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    return runtime_strategy_for_setup_name(getattr(setup, "strategy", ""))
