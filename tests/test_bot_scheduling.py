from datetime import datetime, timezone

from src.bot import next_m15_boundary


def test_next_boundary_from_mid_candle():
    now = datetime(2024, 1, 2, 10, 7, 30, tzinfo=timezone.utc)
    assert next_m15_boundary(now) == datetime(2024, 1, 2, 10, 15, tzinfo=timezone.utc)


def test_next_boundary_exactly_on_boundary_rolls_to_next():
    now = datetime(2024, 1, 2, 10, 15, 0, tzinfo=timezone.utc)
    assert next_m15_boundary(now) == datetime(2024, 1, 2, 10, 30, tzinfo=timezone.utc)


def test_next_boundary_crosses_hour():
    now = datetime(2024, 1, 2, 10, 46, tzinfo=timezone.utc)
    assert next_m15_boundary(now) == datetime(2024, 1, 2, 11, 0, tzinfo=timezone.utc)
