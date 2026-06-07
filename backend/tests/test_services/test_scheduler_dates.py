"""Tests for scheduler date generation with defensive bounds."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

from app.tasks.scheduler import FlightScheduler


def make_scheduler(scrape_max_days_ahead: int = 365) -> FlightScheduler:
    settings = MagicMock()
    settings.scheduler_enabled = False
    settings.telegram_bot_token = ""
    settings.telegram_chat_id = ""
    settings.sentry_dsn = ""
    # Real int so the rolling-horizon cap in _group_dates doesn't clamp existing
    # date-range tests (default high). Horizon behaviour has its own tests below.
    settings.scrape_max_days_ahead = scrape_max_days_ahead
    return FlightScheduler(
        settings=settings,
        session_factory=MagicMock(),
        provider_registry=MagicMock(),
    )


def make_group(
    days_ahead: int = 30,
    start_date: date | None = None,
    end_date: date | None = None,
) -> MagicMock:
    group = MagicMock()
    group.days_ahead = days_ahead
    group.start_date = start_date
    group.end_date = end_date
    return group


def test_group_dates_normal_range() -> None:
    scheduler = make_scheduler()
    group = make_group(days_ahead=7)
    dates = scheduler._group_dates(group)
    assert len(dates) == 7
    assert dates[0] == date.today()
    assert dates[-1] == date.today() + timedelta(days=6)


def test_group_dates_explicit_range() -> None:
    # Use future-relative dates so the range is never clamped to today()
    # (the scheduler sets start = max(start_date, today)). Hardcoded calendar
    # dates made this test fail once "today" passed them.
    scheduler = make_scheduler()
    start = date.today() + timedelta(days=10)
    end = start + timedelta(days=4)
    group = make_group(start_date=start, end_date=end)
    dates = scheduler._group_dates(group)
    assert len(dates) == 5
    assert dates[0] == start
    assert dates[-1] == end


def test_group_dates_capped_at_730() -> None:
    """The _MAX_DATES=730 hard safety ceiling still applies. In production the
    325-day rolling horizon caps first; here we disable the horizon to verify the
    deeper 730 guardrail in isolation."""
    scheduler = make_scheduler(scrape_max_days_ahead=0)
    group = make_group(days_ahead=5000)
    dates = scheduler._group_dates(group)
    assert len(dates) == 730


def test_group_dates_explicit_end_capped_at_730() -> None:
    """An explicit far-future end_date is still capped at the 730 safety ceiling
    (horizon disabled here to verify that ceiling in isolation)."""
    scheduler = make_scheduler(scrape_max_days_ahead=0)
    group = make_group(
        start_date=date(2026, 1, 1),
        end_date=date(2030, 1, 1),  # ~4 years
    )
    dates = scheduler._group_dates(group)
    assert len(dates) == 730


def test_group_dates_capped_at_horizon() -> None:
    """The rolling horizon caps the date range at today + scrape_max_days_ahead.
    This is the limit that actually applies in production (325 days)."""
    scheduler = make_scheduler(scrape_max_days_ahead=325)
    group = make_group(days_ahead=5000)  # group wants way more than the horizon
    dates = scheduler._group_dates(group)
    assert dates[0] == date.today()
    assert dates[-1] == date.today() + timedelta(days=325)
    assert len(dates) == 326  # today through today+325 inclusive


def test_group_dates_horizon_beats_730() -> None:
    """When both limits could apply, the smaller 325 horizon wins (production)."""
    scheduler = make_scheduler(scrape_max_days_ahead=325)
    group = make_group(
        start_date=date.today(),
        end_date=date.today() + timedelta(days=4000),
    )
    dates = scheduler._group_dates(group)
    assert len(dates) == 326  # horizon (325) caps first, not 730


def test_group_dates_zero_days_ahead() -> None:
    scheduler = make_scheduler()
    group = make_group(days_ahead=0)
    dates = scheduler._group_dates(group)
    assert len(dates) == 1
    assert dates[0] == date.today()


def test_group_dates_one_day() -> None:
    scheduler = make_scheduler()
    group = make_group(days_ahead=1)
    dates = scheduler._group_dates(group)
    assert len(dates) == 1
    assert dates[0] == date.today()


def test_group_dates_clamps_past_start_to_today() -> None:
    scheduler = make_scheduler()
    group = make_group(
        start_date=date.today() - timedelta(days=10),
        end_date=date.today() + timedelta(days=2),
    )
    dates = scheduler._group_dates(group)
    assert dates[0] == date.today()
    assert dates[-1] == date.today() + timedelta(days=2)


def test_group_dates_returns_empty_when_explicit_end_is_in_past() -> None:
    scheduler = make_scheduler()
    group = make_group(
        start_date=date.today() - timedelta(days=10),
        end_date=date.today() - timedelta(days=1),
    )
    assert scheduler._group_dates(group) == []
