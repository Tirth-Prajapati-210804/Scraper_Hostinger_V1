from __future__ import annotations

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.collection_run import CollectionRun
from app.tasks.scheduler import FlightScheduler

TODAY = date.today()
D1 = TODAY + timedelta(days=1)
D2 = TODAY + timedelta(days=2)
D3 = TODAY + timedelta(days=3)


def _filter_stub(*, plan: list[date], summary: list[date], pause: list[date]) -> AsyncMock:
    """Stub for _filter_already_scraped that returns the right value per caller,
    robust to how many segments each path iterates.

    The planning + summary passes use the small PLANNED date list; the auto-pause
    exhaustion check (_pause_group_if_exhausted) passes the group's FULL date
    range. We tell them apart by call order: the first call is planning, then
    summary calls return `summary`, and any call whose `dates` arg is longer than
    the planned set is the pause check and returns `pause`.
    """
    planned_len = len(plan)
    state = {"first": True}

    async def _impl(*args, **kwargs):
        dates = kwargs.get("dates", [])
        if state["first"]:
            state["first"] = False
            return list(plan)
        # The pause check scans the full range (more dates than were planned).
        if len(dates) > planned_len:
            return list(pause)
        return list(summary)

    return AsyncMock(side_effect=_impl)


def make_scheduler() -> FlightScheduler:
    settings = MagicMock()
    settings.scheduler_enabled = False
    settings.telegram_bot_token = ""
    settings.telegram_chat_id = ""
    settings.sentry_dsn = ""
    settings.scrape_no_fare_skip_hours = 0
    settings.scrape_max_empty_attempts = 2
    settings.scrape_max_error_attempts = 2
    settings.scrape_error_cap_since = ""  # disabled by default (no surprise skips)
    return FlightScheduler(
        settings=settings,
        session_factory=MagicMock(),
        provider_registry=MagicMock(),
    )


def make_execute_result(rows: list[tuple]) -> MagicMock:
    result = MagicMock()
    result.fetchall.return_value = rows
    return result


ROUTE_ID = "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_partial_destination_not_excluded() -> None:
    """Date with only 1 of 2 destinations collected must NOT be filtered out."""
    scheduler = make_scheduler()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=make_execute_result([(D1, "SGN")]))

    remaining = await scheduler._filter_already_scraped(
        session, ROUTE_ID, "YYZ", ["SGN", "HAN"], [D1, D2]
    )

    assert D1 in remaining
    assert D2 in remaining


@pytest.mark.asyncio
async def test_all_destinations_excludes_date() -> None:
    """Date with all destinations collected IS excluded."""
    scheduler = make_scheduler()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=make_execute_result([(D1, "SGN"), (D1, "HAN")]))

    remaining = await scheduler._filter_already_scraped(
        session, ROUTE_ID, "YYZ", ["SGN", "HAN"], [D1, D2]
    )

    assert D1 not in remaining
    assert D2 in remaining


@pytest.mark.asyncio
async def test_all_dates_fully_scraped_returns_empty() -> None:
    """If every date is fully collected, the returned list is empty."""
    scheduler = make_scheduler()
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=make_execute_result([(D1, "SGN"), (D2, "SGN")])
    )

    remaining = await scheduler._filter_already_scraped(
        session, ROUTE_ID, "YYZ", ["SGN"], [D1, D2]
    )

    assert remaining == []


@pytest.mark.asyncio
async def test_no_scrapes_returns_all_dates() -> None:
    """If nothing was collected yet, all dates are returned unchanged."""
    scheduler = make_scheduler()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=make_execute_result([]))

    dates = [D1, D2, D3]
    remaining = await scheduler._filter_already_scraped(
        session, ROUTE_ID, "YYZ", ["SGN", "HAN"], dates
    )

    assert remaining == dates


@pytest.mark.asyncio
async def test_empty_dates_parked_after_attempt_cap() -> None:
    """A (date,destination) that has come back empty >= scrape_max_empty_attempts
    times is parked (stops auto-retrying). Covers both filtered_out and the
    genuinely-empty page_empty case (the credit leak)."""
    scheduler = make_scheduler()
    scheduler.settings.scrape_max_empty_attempts = 2
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            make_execute_result([]),               # no saved fares
            make_execute_result([(D1, "MLA")]),    # D1 hit the attempt cap -> parked
        ]
    )

    remaining = await scheduler._filter_already_scraped(
        session, ROUTE_ID, "DEN", ["MLA"], [D1, D2]
    )

    assert D1 not in remaining  # parked (reached attempt cap)
    assert D2 in remaining
    no_fare_sql = str(session.execute.await_args_list[1].args[0])
    no_fare_params = session.execute.await_args_list[1].args[1]
    # Count-based cap, covering both empty reasons; no time window.
    assert "page_empty" in no_fare_sql
    assert "filtered_out" in no_fare_sql
    assert "COUNT(*) >= :max_attempts" in no_fare_sql
    assert "make_interval" not in no_fare_sql  # no clock
    assert no_fare_params["max_attempts"] == 2


@pytest.mark.asyncio
async def test_error_cap_skipped_when_cap_since_unset() -> None:
    """With scrape_error_cap_since empty, the error brake is OFF (fail-safe):
    only the saved-fare + empty-cap queries run, never the error query. This is
    what protects existing live data from surprise skips before deploy."""
    scheduler = make_scheduler()
    scheduler.settings.scrape_error_cap_since = ""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=make_execute_result([]))

    remaining = await scheduler._filter_already_scraped(
        session, ROUTE_ID, "DEN", ["MLA"], [D1]
    )

    assert remaining == [D1]
    # saved-fare query + empty-cap query only (no error query).
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_transient_and_hard_errors_parked_with_cutoff() -> None:
    """Once cap_since is set, transient errors are capped at scrape_max_error_attempts
    and hard errors at 1, and the query is scoped to created_at >= cutoff."""
    scheduler = make_scheduler()
    scheduler.settings.scrape_max_empty_attempts = 0  # isolate the error query
    scheduler.settings.scrape_max_error_attempts = 2
    scheduler.settings.scrape_error_cap_since = "2026-06-03T00:00:00Z"
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            make_execute_result([]),               # no saved fares
            make_execute_result([(D1, "MLA")]),    # D1 reached an error cap -> parked
        ]
    )

    remaining = await scheduler._filter_already_scraped(
        session, ROUTE_ID, "DEN", ["MLA"], [D1, D2]
    )

    assert D1 not in remaining
    assert D2 in remaining
    err_sql = str(session.execute.await_args_list[1].args[0])
    err_params = session.execute.await_args_list[1].args[1]
    assert "provider_error" in err_sql
    assert "extract_failed" in err_sql
    assert "rate_limited" in err_sql
    assert "market_mismatch" in err_sql
    assert "created_at >= :cap_since" in err_sql
    assert err_params["max_attempts"] == 2
    assert err_params["cap_since"].year == 2026


@pytest.mark.asyncio
async def test_provider_block_halts_run_without_parking_dates() -> None:
    """quota/auth failures must set _stop_requested (halt) and still report to
    the registry -- but they do NOT park the date (that's the cap query's job)."""
    from app.providers.base import ProviderAuthError, ProviderQuotaExhaustedError

    scheduler = make_scheduler()
    scheduler.provider_registry.report_failure = MagicMock()

    assert scheduler._stop_requested is False
    scheduler._on_provider_failure("scrapingbee", ProviderQuotaExhaustedError("out"))
    assert scheduler._stop_requested is True
    scheduler.provider_registry.report_failure.assert_called_once()

    scheduler._stop_requested = False
    scheduler._on_provider_failure("scrapingbee", ProviderAuthError("bad key"))
    assert scheduler._stop_requested is True


@pytest.mark.asyncio
async def test_provider_failure_non_block_does_not_halt() -> None:
    """A plain provider_error must NOT halt the run -- it's a per-date issue."""
    scheduler = make_scheduler()
    scheduler.provider_registry.report_failure = MagicMock()

    scheduler._on_provider_failure("scrapingbee", RuntimeError("timeout"))

    assert scheduler._stop_requested is False
    scheduler.provider_registry.report_failure.assert_called_once()


@pytest.mark.asyncio
async def test_empty_date_attempt_cap_disabled_when_zero() -> None:
    scheduler = make_scheduler()
    scheduler.settings.scrape_max_empty_attempts = 0
    session = AsyncMock()
    session.execute = AsyncMock(return_value=make_execute_result([]))

    remaining = await scheduler._filter_already_scraped(
        session, ROUTE_ID, "DEN", ["MLA"], [D1]
    )

    assert remaining == [D1]
    # With cap=0 the no-fare query is skipped entirely (only the saved-fare query runs).
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_no_fare_skip_does_not_double_count_same_destination() -> None:
    scheduler = make_scheduler()
    scheduler.settings.scrape_max_empty_attempts = 2
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            make_execute_result([(D1, "MLA")]),
            make_execute_result([(D1, "MLA")]),
        ]
    )

    remaining = await scheduler._filter_already_scraped(
        session, ROUTE_ID, "DEN", ["MLA", "FCO"], [D1]
    )

    assert remaining == [D1]


@pytest.mark.asyncio
async def test_no_fare_skip_can_be_disabled() -> None:
    # Setting the attempt cap to 0 disables the empty-date brake entirely.
    scheduler = make_scheduler()
    scheduler.settings.scrape_max_empty_attempts = 0
    session = AsyncMock()
    session.execute = AsyncMock(return_value=make_execute_result([]))

    remaining = await scheduler._filter_already_scraped(
        session, ROUTE_ID, "DEN", ["MLA"], [D1]
    )

    assert remaining == [D1]
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_filter_is_scoped_to_route_group() -> None:
    scheduler = make_scheduler()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=make_execute_result([]))

    await scheduler._filter_already_scraped(
        session, ROUTE_ID, "YYZ", ["SGN", "HAN"], [D1, D2]
    )

    params = session.execute.await_args.args[1]
    assert params["route_group_id"] == ROUTE_ID


@pytest.mark.asyncio
async def test_trigger_single_group_forwards_trip_type_and_nights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: trigger_single_group must pass the route group's trip_type
    and nights into the collector, otherwise round-trip groups silently get
    scraped as one-way and the export shows wrong fares."""
    from uuid import uuid4

    from app.tasks import scheduler as scheduler_module

    scheduler = make_scheduler()
    scheduler.settings.scrape_batch_size = 2
    scheduler.settings.scrape_delay_seconds = 0.0

    group = MagicMock()
    group.id = uuid4()
    group.is_active = True
    group.origins = ["YYZ"]
    group.destinations = ["NRT"]
    group.currency = "USD"
    group.market = "ca"
    group.max_stops = None
    group.same_airline_only = True
    group.max_leg_duration_minutes = None
    group.trip_type = "round_trip"
    group.nights = 14
    group.start_date = None
    group.end_date = None
    group.days_ahead = 7

    captured: dict = {}

    class DummyCollector:
        def __init__(self, *a, **kw) -> None:
            pass

        async def collect_route_batch(self, **kwargs):
            captured.update(kwargs)
            return {"success": 0, "errors": 0, "skipped": 0}

    monkeypatch.setattr(scheduler_module, "PriceCollector", DummyCollector)

    fake_provider = MagicMock()
    fake_provider.is_configured.return_value = True
    scheduler.provider_registry.get_enabled = MagicMock(return_value=[fake_provider])

    # Stub the DB lookup of the group and the freshness filter.
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = group
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(return_value=select_result)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    scheduler.session_factory = factory

    # plan -> [D1] (one pending date); summary -> [] (it got collected); the
    # auto-pause exhaustion check that follows returns [D1] = "still has work",
    # so the group is NOT auto-paused in this test.
    scheduler._filter_already_scraped = _filter_stub(plan=[D1], summary=[], pause=[D1])

    await scheduler.trigger_single_group(group.id)

    assert captured["trip_type"] == "round_trip"
    assert captured["nights"] == 14
    assert captured["market"] == "ca"
    assert captured["currency"] == "USD"
    assert captured["same_airline_only"] is True


@pytest.mark.asyncio
async def test_trigger_single_group_collects_multi_city_special_sheets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from app.tasks import scheduler as scheduler_module

    scheduler = make_scheduler()
    scheduler.settings.scrape_batch_size = 2
    scheduler.settings.scrape_delay_seconds = 0.0

    group = MagicMock()
    group.id = uuid4()
    group.is_active = True
    group.origins = ["YYZ"]
    group.destinations = ["BER"]
    group.currency = "USD"
    group.market = "ca"
    group.max_stops = None
    group.same_airline_only = False
    group.max_leg_duration_minutes = None
    group.trip_type = "multi_city"
    group.nights = 7
    group.start_date = None
    group.end_date = None
    group.days_ahead = 5
    group.special_sheets = [
        {
            "name": "Return Leg",
            "origin": "BUD",
            "destination_label": "Toronto",
            "destinations": ["YYZ"],
            "columns": 4,
        }
    ]

    captured: list[dict] = []

    class DummyCollector:
        def __init__(self, *a, **kw) -> None:
            pass

        async def collect_route_batch(self, **kwargs):
            captured.append(kwargs)
            return {"success": 0, "errors": 0, "skipped": 0}

    monkeypatch.setattr(scheduler_module, "PriceCollector", DummyCollector)

    fake_provider = MagicMock()
    fake_provider.is_configured.return_value = True
    scheduler.provider_registry.get_enabled = MagicMock(return_value=[fake_provider])

    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = group
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(return_value=select_result)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    scheduler.session_factory = factory

    scheduler._filter_already_scraped = _filter_stub(
        plan=[D1, D2, D3, D3 + timedelta(days=1), D3 + timedelta(days=2)],
        summary=[],
        # auto-pause exhaustion check -> still has work, so no auto-pause here.
        pause=[D1],
    )

    await scheduler.trigger_single_group(group.id)

    assert len(captured) == 1
    assert captured[0]["origin"] == "YYZ"
    assert captured[0]["destinations"] == ["BER"]
    assert captured[0]["trip_type"] == "multi_city"
    assert captured[0]["nights"] == 7
    assert captured[0]["market"] == "ca"
    assert captured[0]["same_airline_only"] is False
    assert captured[0]["return_origin"] == "BUD"
    assert captured[0]["batch_size"] == 2
    assert callable(captured[0]["stop_check"])


@pytest.mark.asyncio
async def test_trigger_single_group_updates_live_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from app.tasks import scheduler as scheduler_module

    scheduler = make_scheduler()
    scheduler.settings.scrape_batch_size = 2
    scheduler.settings.scrape_delay_seconds = 0.0

    group = MagicMock()
    group.id = uuid4()
    group.is_active = True
    group.origins = ["AMD"]
    group.destinations = ["YYZ"]
    group.currency = "USD"
    group.market = "us"
    group.max_stops = None
    group.same_airline_only = False
    group.max_leg_duration_minutes = None
    group.trip_type = "round_trip"
    group.nights = 0
    group.start_date = None
    group.end_date = None
    group.days_ahead = 7

    class DummyCollector:
        def __init__(self, *a, **kw) -> None:
            self.on_item_progress = kw["on_item_progress"]

        async def collect_route_batch(self, **kwargs):
            self.on_item_progress("success", "AMD", "YYZ", D1, False)
            self.on_item_progress("skipped", "AMD", "YYZ", D2, False)
            return {"success": 1, "errors": 0, "skipped": 1}

    monkeypatch.setattr(scheduler_module, "PriceCollector", DummyCollector)

    fake_provider = MagicMock()
    fake_provider.is_configured.return_value = True
    scheduler.provider_registry.get_enabled = MagicMock(return_value=[fake_provider])

    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = group
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(return_value=select_result)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    scheduler.session_factory = factory

    # plan -> [D1, D2]; summary -> []; auto-pause exhaustion check -> [D1]
    # (still has work) so the group is NOT auto-paused here.
    scheduler._filter_already_scraped = _filter_stub(plan=[D1, D2], summary=[], pause=[D1])

    stats = await scheduler.trigger_single_group(group.id)

    assert stats == {"success": 1, "errors": 0, "skipped": 1}
    assert scheduler.progress["routes_total"] == 1
    assert scheduler.progress["routes_done"] == 1
    assert scheduler.progress["prices_total"] == 2
    assert scheduler.progress["prices_done"] == 2
    assert scheduler.progress["dates_scraped"] == 1
    assert scheduler.progress["current_origin"] == "AMD"
    assert scheduler.progress["current_destination"] == "YYZ"
    assert scheduler.progress["current_date"] == D2.isoformat()


@pytest.mark.asyncio
async def test_trigger_single_group_clears_stale_errors_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from app.tasks import scheduler as scheduler_module

    scheduler = make_scheduler()
    scheduler.settings.scrape_batch_size = 1
    scheduler.settings.scrape_delay_seconds = 0.0

    group = MagicMock()
    group.id = uuid4()
    group.is_active = True
    group.origins = ["YVR"]
    group.destinations = ["DPS"]
    group.currency = "USD"
    group.market = "ca"
    group.max_stops = 1
    group.same_airline_only = True
    group.max_leg_duration_minutes = None
    group.trip_type = "multi_city"
    group.nights = 12
    group.start_date = None
    group.end_date = None
    group.days_ahead = 30
    group.special_sheets = [
        {
            "name": "SIN-YVR",
            "origin": "SIN",
            "destination_label": "YVR",
            "destinations": ["YVR"],
            "columns": 4,
        }
    ]

    class DummyCollector:
        def __init__(self, *a, **kw) -> None:
            pass

        async def collect_route_batch(self, **kwargs):
            return {"success": 1, "errors": 0, "skipped": 0}

    monkeypatch.setattr(scheduler_module, "PriceCollector", DummyCollector)

    fake_provider = MagicMock()
    fake_provider.is_configured.return_value = True
    scheduler.provider_registry.get_enabled = MagicMock(return_value=[fake_provider])

    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = group
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(return_value=select_result)

    captured_runs: list = []

    def capture_add(model) -> None:
        if model.__class__.__name__ == "CollectionRun":
            model.errors = ["Server restarted mid-collection"]
            captured_runs.append(model)

    session.add.side_effect = capture_add

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    scheduler.session_factory = factory

    # plan -> [D1]; summary -> []; auto-pause exhaustion check -> [D1]
    # (still has work) so the group is NOT auto-paused here.
    scheduler._filter_already_scraped = _filter_stub(plan=[D1], summary=[], pause=[D1])

    await scheduler.trigger_single_group(group.id)

    assert captured_runs
    assert captured_runs[0].status == "completed"
    assert captured_runs[0].errors == []


@pytest.mark.asyncio
async def test_scheduler_uses_dedicated_connection_for_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import scheduler as scheduler_module

    scheduler = make_scheduler()

    connection = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar.return_value = True
    connection.execute = AsyncMock(return_value=execute_result)
    connection.close = AsyncMock()

    bind = MagicMock()
    bind.connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(scheduler_module, "AsyncEngine", MagicMock)
    scheduler.session_factory = MagicMock(kw={"bind": bind})

    session = AsyncMock()

    acquired = await scheduler._acquire_global_lock(session)

    assert acquired is True
    assert scheduler._lock_connection is connection
    session.execute.assert_not_called()

    await scheduler._release_global_lock(session)

    connection.execute.assert_awaited()
    connection.close.assert_awaited()
    assert scheduler._lock_connection is None


@pytest.mark.asyncio
async def test_start_collection_task_tracks_one_active_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = make_scheduler()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_collection_cycle() -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(scheduler, "run_collection_cycle", fake_run_collection_cycle)

    assert scheduler.start_collection_task() is True
    await started.wait()
    assert scheduler._active_task is not None
    assert scheduler.start_collection_task() is False

    release.set()
    await scheduler._active_task
    assert scheduler._active_task is None


@pytest.mark.asyncio
async def test_start_single_group_task_tracks_one_active_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = make_scheduler()
    group_id = uuid4()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_trigger_single_group(
        passed_group_id,
        target_dates=None,
    ) -> dict[str, int]:
        assert passed_group_id == group_id
        assert target_dates == [D1]
        started.set()
        await release.wait()
        return {"success": 0, "errors": 0, "skipped": 0}

    monkeypatch.setattr(scheduler, "trigger_single_group", fake_trigger_single_group)

    assert scheduler.start_single_group_task(group_id, [D1]) is True
    await started.wait()
    assert scheduler._active_task is not None
    assert scheduler.start_single_group_task(group_id, [D1]) is False

    release.set()
    await scheduler._active_task
    assert scheduler._active_task is None


@pytest.mark.asyncio
async def test_recover_incomplete_all_collection_restarts_latest_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = make_scheduler()
    stale_run = CollectionRun(
        id=uuid4(),
        status="running",
        errors=[{"code": "run_context", "mode": "all"}],
    )
    older_run = CollectionRun(
        id=uuid4(),
        status="running",
        errors=[{"code": "run_context", "mode": "single_group", "group_id": str(uuid4())}],
    )

    result = MagicMock()
    scalar_result = MagicMock()
    scalar_result.all.return_value = [stale_run, older_run]
    result.scalars.return_value = scalar_result

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    scheduler.session_factory = factory

    start_collection = MagicMock(return_value=True)
    start_single_group = MagicMock(return_value=False)
    monkeypatch.setattr(scheduler, "start_collection_task", start_collection)
    monkeypatch.setattr(scheduler, "start_single_group_task", start_single_group)

    resumed = await scheduler.recover_incomplete_run()

    assert resumed is True
    start_collection.assert_called_once_with()
    start_single_group.assert_not_called()
    session.commit.assert_awaited_once()
    assert stale_run.status == "failed"
    assert stale_run.errors[0]["code"] == "restarted_mid_collection"
    assert older_run.status == "failed"
    assert older_run.errors[0]["code"] == "superseded_by_recovery"


@pytest.mark.asyncio
async def test_recover_incomplete_single_group_restarts_same_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = make_scheduler()
    group_id = uuid4()
    stale_run = CollectionRun(
        id=uuid4(),
        status="running",
        errors=[
            {
                "code": "run_context",
                "mode": "single_group",
                "group_id": str(group_id),
                "target_dates": [D1.isoformat(), D2.isoformat()],
            }
        ],
    )

    result = MagicMock()
    scalar_result = MagicMock()
    scalar_result.all.return_value = [stale_run]
    result.scalars.return_value = scalar_result

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    scheduler.session_factory = factory

    start_collection = MagicMock(return_value=False)
    start_single_group = MagicMock(return_value=True)
    monkeypatch.setattr(scheduler, "start_collection_task", start_collection)
    monkeypatch.setattr(scheduler, "start_single_group_task", start_single_group)

    resumed = await scheduler.recover_incomplete_run()

    assert resumed is True
    start_collection.assert_not_called()
    start_single_group.assert_called_once_with(group_id, [D1, D2])
    session.commit.assert_awaited_once()
    assert stale_run.status == "failed"
    assert stale_run.errors[0]["code"] == "restarted_mid_collection"


def test_route_parallelism_caps_groups_by_provider_budget() -> None:
    scheduler = make_scheduler()
    scheduler.settings.scrape_route_parallelism = 5
    scheduler.settings.provider_concurrency_limit = 5
    scheduler.settings.scrape_batch_size = 1

    assert scheduler._route_parallelism(10) == 5
    assert scheduler._route_parallelism(3) == 3

    scheduler.settings.scrape_batch_size = 2
    assert scheduler._route_parallelism(10) == 2


def _exhaustion_group() -> MagicMock:
    group = MagicMock()
    group.id = uuid4()
    group.name = "YYZ -> Iceland"
    group.is_active = True
    group.origins = ["YYZ"]
    group.destinations = ["KEF"]
    group.trip_type = "round_trip"
    group.nights = 5
    group.special_sheets = []
    group.start_date = None
    group.end_date = None
    group.days_ahead = 3
    return group


def _exhaustion_scheduler(group: MagicMock) -> FlightScheduler:
    """A scheduler whose session_factory yields a session whose .get() returns
    the given group, so _pause_group_if_exhausted can re-fetch + flip it."""
    scheduler = make_scheduler()
    session = AsyncMock()
    session.get = AsyncMock(return_value=group)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    scheduler.session_factory = factory
    return scheduler


@pytest.mark.asyncio
async def test_pause_group_if_exhausted_pauses_when_nothing_remains() -> None:
    """When every date is collected or capped (filter returns []), the group is
    auto-paused (is_active -> False) so it drops out of scheduled runs."""
    group = _exhaustion_group()
    scheduler = _exhaustion_scheduler(group)
    scheduler._filter_already_scraped = AsyncMock(return_value=[])

    paused = await scheduler._pause_group_if_exhausted(group)

    assert paused is True
    assert group.is_active is False


@pytest.mark.asyncio
async def test_pause_group_if_exhausted_keeps_active_when_work_remains() -> None:
    """A group with any pending date (filter returns a date) stays active."""
    group = _exhaustion_group()
    scheduler = _exhaustion_scheduler(group)
    scheduler._filter_already_scraped = AsyncMock(return_value=[D1])

    paused = await scheduler._pause_group_if_exhausted(group)

    assert paused is False
    assert group.is_active is True


@pytest.mark.asyncio
async def test_pause_group_if_exhausted_skips_already_paused_group() -> None:
    """An already-paused group is left alone and is not re-checked."""
    group = _exhaustion_group()
    group.is_active = False
    scheduler = _exhaustion_scheduler(group)
    scheduler._filter_already_scraped = AsyncMock(return_value=[])

    paused = await scheduler._pause_group_if_exhausted(group)

    assert paused is False
    scheduler._filter_already_scraped.assert_not_awaited()
