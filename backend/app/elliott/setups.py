def select_wave_strategy(
    current_wave: str,
    direction: str,
    has_aligned_sweep: bool,
    structure_event_types: set[str],
) -> str | None:
    """Return a wave strategy only after its deterministic confirmations exist."""
    if (
        current_wave == "2"
        and has_aligned_sweep
        and {"CHoCH", "BOS"}.issubset(structure_event_types)
    ):
        return f"{direction}_wave_3"
    if current_wave == "4" and "BOS" in structure_event_types:
        return f"{direction}_wave_5"
    if current_wave == "B" and "BOS" in structure_event_types:
        return f"{direction}_c_wave"
    return None
