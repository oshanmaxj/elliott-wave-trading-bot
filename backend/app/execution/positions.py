from decimal import Decimal


def restore_setup_protection_levels(position, setup) -> bool:
    """Restore absent local protection geometry without replacing valid values."""
    if setup is None:
        return False

    changed = False
    if setup.stop_loss is not None and (
        position.stop_loss is None or Decimal(position.stop_loss) <= 0
    ):
        position.stop_loss = setup.stop_loss
        changed = True

    for field in ("take_profit_1", "take_profit_2", "take_profit_3"):
        if getattr(position, field) is None and getattr(setup, field) is not None:
            setattr(position, field, getattr(setup, field))
            changed = True

    return changed
