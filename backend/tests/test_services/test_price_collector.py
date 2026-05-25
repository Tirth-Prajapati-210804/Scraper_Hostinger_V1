from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.providers.base import ProviderSearchDiagnostics, ProviderSearchOutcome, ProviderResult
from app.services.price_collector import CollectionResult, PriceCollector


def make_result(
    price: float,
    airline: str = "AC",
    provider: str = "serpapi",
    *,
    duration_minutes: int = 0,
    stops: int = 0,
    raw_data: dict | None = None,
) -> ProviderResult:
    return ProviderResult(
        price=price,
        currency="CAD",
        airline=airline,
        deep_link="https://example.com",
        provider=provider,
        duration_minutes=duration_minutes,
        stops=stops,
        raw_data=raw_data or {},
    )


def make_provider(name: str, results: list[ProviderResult]) -> MagicMock:
    provider = MagicMock()
    provider.name = name
    provider.search_round_trip = AsyncMock(return_value=results)
    provider.search_round_trip_diagnostic = None
    provider.search_multi_city = AsyncMock(return_value=results)
    provider.search_multi_city_diagnostic = None
    return provider


def make_session_factory(session: AsyncMock) -> MagicMock:
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    return factory


ROUTE_ID = uuid.uuid4()
TODAY = date.today()
DEPART = TODAY + timedelta(days=30)


@pytest.mark.asyncio
async def test_collect_single_date_returns_cheapest() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider("serpapi", [make_result(1500), make_result(2000)])
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()

    result = await collector.collect_single_date("YYZ", "NRT", DEPART, ROUTE_ID)

    assert isinstance(result, CollectionResult)
    assert result.cheapest is not None
    assert result.cheapest.price == 1500
    assert result.origin == "YYZ"
    assert result.destination == "NRT"
    assert result.depart_date == DEPART
    collector._upsert_cheapest.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_single_date_picks_cheapest_across_providers() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    p1 = make_provider("serpapi", [make_result(1800, provider="serpapi")])
    p2 = make_provider("serpapi_b", [make_result(1200, provider="serpapi_b")])
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[p1, p2],
    )
    collector._upsert_cheapest = AsyncMock()

    result = await collector.collect_single_date("YYZ", "NRT", DEPART, ROUTE_ID)

    assert result.cheapest is not None
    assert result.cheapest.price == 1200
    assert result.cheapest.provider == "serpapi_b"


@pytest.mark.asyncio
async def test_collect_single_date_one_provider_fails() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    p_good = make_provider("serpapi", [make_result(1500)])
    p_bad = MagicMock()
    p_bad.name = "serpapi_b"
    p_bad.search_round_trip = AsyncMock(side_effect=RuntimeError("API down"))
    p_bad.search_round_trip_diagnostic = None

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[p_good, p_bad],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    result = await collector.collect_single_date("YYZ", "NRT", DEPART, ROUTE_ID)

    assert result.cheapest is not None
    assert result.cheapest.price == 1500
    assert "serpapi_b" in result.errors
    assert session.add.call_count == 2


@pytest.mark.asyncio
async def test_collect_single_date_reports_provider_health_callbacks() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    p_good = make_provider("searchapi", [make_result(1500, provider="searchapi")])
    p_bad = MagicMock()
    p_bad.name = "searchapi_b"
    p_bad.search_round_trip = AsyncMock(side_effect=RuntimeError("API down"))
    p_bad.search_round_trip_diagnostic = None

    success_cb = MagicMock()
    failure_cb = MagicMock()

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[p_good, p_bad],
        on_provider_success=success_cb,
        on_provider_failure=failure_cb,
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    await collector.collect_single_date("YYZ", "NRT", DEPART, ROUTE_ID)

    success_cb.assert_called_once_with("searchapi")
    failure_cb.assert_called_once()
    assert failure_cb.call_args.args[0] == "searchapi_b"


@pytest.mark.asyncio
async def test_collect_single_date_no_results() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider("kiwi", [])
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()

    result = await collector.collect_single_date("YYZ", "NRT", DEPART, ROUTE_ID)

    assert result.cheapest is None
    collector._upsert_cheapest.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_single_date_records_filtered_out_reason_for_direct_mode() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider("kiwi", [make_result(900, stops=1)])
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()

    result = await collector.collect_single_date(
        "YYZ",
        "NRT",
        DEPART,
        ROUTE_ID,
        currency="CAD",
        max_stops=0,
    )

    assert result.cheapest is None
    scrape_logs = [call.args[0] for call in session.add.call_args_list if call.args]
    assert any(getattr(log, "result_reason", None) == "filtered_out" for log in scrape_logs)


@pytest.mark.asyncio
async def test_collect_single_date_preserves_provider_raw_offer_count() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "scrapingbee"
    provider.search_round_trip_diagnostic = AsyncMock(
        return_value=ProviderSearchOutcome(
            results=[],
            diagnostics=ProviderSearchDiagnostics(
                result_reason="filtered_out",
                raw_offers_found=37,
                eligible_offers_found=0,
                visible_results_found=True,
                summary_price_found=True,
            ),
        )
    )

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()

    result = await collector.collect_single_date(
        "DEN",
        "MLA",
        DEPART,
        ROUTE_ID,
        currency="USD",
        market="us",
        max_stops=1,
    )

    assert result.cheapest is None
    scrape_logs = [call.args[0] for call in session.add.call_args_list if call.args]
    assert any(
        getattr(log, "result_reason", None) == "filtered_out"
        and getattr(log, "raw_offers_found", None) == 37
        for log in scrape_logs
    )


@pytest.mark.asyncio
async def test_collect_single_date_direct_mode_chooses_direct_offer_only() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider(
        "kiwi",
        [
            make_result(700, stops=1, duration_minutes=400),
            make_result(725, stops=0, duration_minutes=410),
        ],
    )
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    result = await collector.collect_single_date(
        "YYZ",
        "NRT",
        DEPART,
        ROUTE_ID,
        max_stops=0,
    )

    assert result.cheapest is not None
    assert result.cheapest.stops == 0
    assert result.cheapest.price == 725


@pytest.mark.asyncio
async def test_collect_single_date_records_parse_error_status() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "searchapi"
    provider.search_round_trip = AsyncMock(side_effect=RuntimeError("invalid json from provider"))
    provider.search_round_trip_diagnostic = None

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()

    result = await collector.collect_single_date("YYZ", "NRT", DEPART, ROUTE_ID)

    assert result.cheapest is None
    assert result.errors == {"searchapi": "invalid json from provider"}
    scrape_logs = [call.args[0] for call in session.add.call_args_list if call.args]
    assert any(getattr(log, "status", None) == "parse_error" for log in scrape_logs)


@pytest.mark.asyncio
async def test_collect_single_date_records_provider_error_status() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "searchapi"
    provider.search_round_trip = AsyncMock(side_effect=RuntimeError("provider blew up"))
    provider.search_round_trip_diagnostic = None

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()

    result = await collector.collect_single_date("YYZ", "NRT", DEPART, ROUTE_ID)

    assert result.cheapest is None
    assert result.errors == {"searchapi": "provider blew up"}
    scrape_logs = [call.args[0] for call in session.add.call_args_list if call.args]
    assert any(getattr(log, "status", None) == "provider_error" for log in scrape_logs)


@pytest.mark.asyncio
async def test_collect_route_batch_stats() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider("serpapi", [make_result(1500)])
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()

    dates = [DEPART + timedelta(days=i) for i in range(3)]
    stats = await collector.collect_route_batch(
        origin="YYZ",
        destinations=["NRT"],
        dates=dates,
        route_group_id=ROUTE_ID,
        batch_size=3,
        delay_seconds=0,
    )

    assert stats["success"] == 3
    assert stats["errors"] == 0


@pytest.mark.asyncio
async def test_collect_route_batch_reports_started_before_result() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider("serpapi", [make_result(1500)])
    started_calls: list[tuple[str, str, date, bool]] = []
    progress_calls: list[tuple[str, str, str, date, bool]] = []
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
        on_item_started=lambda origin, destination, depart_date, is_retry: started_calls.append(
            (origin, destination, depart_date, is_retry)
        ),
        on_item_progress=lambda status, origin, destination, depart_date, is_retry: progress_calls.append(
            (status, origin, destination, depart_date, is_retry)
        ),
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    stats = await collector.collect_route_batch(
        origin="YYZ",
        destinations=["NRT"],
        dates=[DEPART],
        route_group_id=ROUTE_ID,
        batch_size=1,
        delay_seconds=0,
    )

    assert stats == {"success": 1, "errors": 0, "skipped": 0}
    assert started_calls == [("YYZ", "NRT", DEPART, False)]
    assert progress_calls == [("success", "YYZ", "NRT", DEPART, False)]


@pytest.mark.asyncio
async def test_collect_route_batch_cooled_route_reports_skipped_progress() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider("serpapi", [make_result(1500)])
    progress_calls: list[tuple[str, str, str, date, bool]] = []
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
        on_item_progress=lambda status, origin, destination, depart_date, is_retry: progress_calls.append(
            (status, origin, destination, depart_date, is_retry)
        ),
    )
    collector._upsert_cheapest = AsyncMock()
    collector._route_cooldown[collector._route_key("YYZ", "NRT")] = 1

    stats = await collector.collect_route_batch(
        origin="YYZ",
        destinations=["NRT"],
        dates=[DEPART],
        route_group_id=ROUTE_ID,
        batch_size=1,
        delay_seconds=0,
    )

    assert stats == {"success": 0, "errors": 0, "skipped": 1}
    assert progress_calls == [("skipped", "YYZ", "NRT", DEPART, False)]
    provider.search_round_trip.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_route_batch_no_results_do_not_cool_later_dates() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider("serpapi", [])
    progress_calls: list[tuple[str, str, str, date, bool]] = []
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
        on_item_progress=lambda status, origin, destination, depart_date, is_retry: progress_calls.append(
            (status, origin, destination, depart_date, is_retry)
        ),
    )
    collector._upsert_cheapest = AsyncMock()

    dates = [DEPART + timedelta(days=i) for i in range(5)]
    stats = await collector.collect_route_batch(
        origin="YYZ",
        destinations=["NRT"],
        dates=dates,
        route_group_id=ROUTE_ID,
        batch_size=1,
        delay_seconds=0,
    )

    assert stats == {"success": 0, "errors": 0, "skipped": 5}
    assert provider.search_round_trip.await_count == 5
    assert [call[0] for call in progress_calls] == ["skipped"] * 5


@pytest.mark.asyncio
async def test_collect_route_batch_cancels_inflight_scrape_when_stop_requested() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider("serpapi", [make_result(1500)])
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_collect_single_date(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    collector.collect_single_date = AsyncMock(side_effect=fake_collect_single_date)

    stop_requested = False

    batch_task = asyncio.create_task(
        collector.collect_route_batch(
            origin="YYZ",
            destinations=["NRT"],
            dates=[DEPART],
            route_group_id=ROUTE_ID,
            batch_size=1,
            delay_seconds=0,
            stop_check=lambda: stop_requested,
        )
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    stop_requested = True

    stats = await asyncio.wait_for(batch_task, timeout=2)

    assert stats == {"success": 0, "errors": 0, "skipped": 1}
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_upsert_cheapest_sends_correct_params() -> None:
    session = AsyncMock()
    session.execute = AsyncMock()

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[],
    )
    result = make_result(1250, airline="AC", provider="serpapi")
    result.deep_link = "https://example.com/booking"

    await collector._upsert_cheapest(
        session=session,
        route_group_id=ROUTE_ID,
        origin="YYZ",
        destination="NRT",
        depart_date=DEPART,
        result=result,
    )

    session.execute.assert_awaited_once()
    call_args = session.execute.call_args[0]
    params = call_args[1]
    assert params["origin"] == "YYZ"
    assert params["destination"] == "NRT"
    assert params["price"] == 1250
    assert params["provider"] == "serpapi"
    assert params["airline"] == "Air Canada"
    assert "WHERE daily_cheapest_prices.price > EXCLUDED.price" not in str(call_args[0])


@pytest.mark.asyncio
async def test_round_trip_calls_search_round_trip_with_return_date() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "searchapi"
    provider.search_round_trip = AsyncMock(
        return_value=[make_result(2400, provider="searchapi")]
    )
    provider.search_round_trip_diagnostic = None

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    result = await collector.collect_single_date(
        origin="YYZ",
        destination="NRT",
        depart_date=DEPART,
        route_group_id=ROUTE_ID,
        trip_type="round_trip",
        nights=10,
    )

    provider.search_round_trip.assert_awaited_once()
    kwargs = provider.search_round_trip.call_args.kwargs
    assert kwargs["depart_date"] == DEPART
    assert kwargs["return_date"] == DEPART + timedelta(days=11)
    assert result.cheapest is not None
    assert result.cheapest.price == 2400


@pytest.mark.asyncio
async def test_collect_single_date_same_airline_only_filters_before_choosing_cheapest() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "searchapi"
    provider.search_round_trip = AsyncMock(
        return_value=[
            make_result(
                1000,
                airline="WestJet, Air Transat",
                provider="searchapi",
                duration_minutes=580,
                stops=1,
                raw_data={"outbound_airline": "WestJet", "return_airline": "Air Transat"},
            ),
            make_result(
                1100,
                airline="Air Canada / Lufthansa",
                provider="searchapi",
                duration_minutes=500,
                stops=1,
                raw_data={"outbound_airline": "Air Canada", "return_airline": "Lufthansa"},
            ),
            make_result(
                1150,
                airline="Multiple airlines",
                provider="searchapi",
                duration_minutes=610,
                stops=1,
                raw_data={"airline_names": ["Multiple airlines"]},
            ),
            make_result(
                1200,
                airline="Air Canada / Air Canada",
                provider="searchapi",
                duration_minutes=700,
                stops=1,
                raw_data={"outbound_airline": "Air Canada", "return_airline": "Air Canada"},
            ),
            make_result(
                1200,
                airline="Air Canada / AC",
                provider="searchapi",
                duration_minutes=640,
                stops=1,
                raw_data={"outbound_airline": "Air Canada", "return_airline": "AC"},
            ),
        ]
    )
    provider.search_round_trip_diagnostic = None

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    result = await collector.collect_single_date(
        origin="YYZ",
        destination="NRT",
        depart_date=DEPART,
        route_group_id=ROUTE_ID,
        trip_type="round_trip",
        nights=10,
        same_airline_only=True,
    )

    assert result.cheapest is not None
    assert result.cheapest.price == 1200
    assert result.cheapest.duration_minutes == 640


@pytest.mark.asyncio
async def test_collect_single_date_rejects_mixed_airline_kayak_leg_text() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "scrapingbee"
    provider.search_round_trip_diagnostic = AsyncMock(
        return_value=ProviderSearchOutcome(
            results=[
                make_result(
                    900,
                    airline="Lufthansa",
                    provider="scrapingbee",
                    stops=1,
                    raw_data={
                        "airline_names": ["Lufthansa"],
                        "legs": [
                            {"airline": "Lufthansa", "route_text": "Lufthansa, KM Malta Airlines"},
                            {"airline": "Lufthansa", "route_text": "Lufthansa"},
                        ],
                    },
                ),
                make_result(
                    1200,
                    airline="Lufthansa",
                    provider="scrapingbee",
                    stops=1,
                    raw_data={
                        "airline_names": ["Lufthansa"],
                        "legs": [
                            {"airline": "Lufthansa", "route_text": "Lufthansa"},
                            {"airline": "Lufthansa", "route_text": "Lufthansa"},
                        ],
                    },
                ),
            ],
            diagnostics=ProviderSearchDiagnostics(raw_offers_found=2),
        )
    )

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    result = await collector.collect_single_date(
        origin="EWR",
        destination="MLA",
        depart_date=DEPART,
        route_group_id=ROUTE_ID,
        trip_type="round_trip",
        nights=8,
        max_stops=1,
    )

    assert result.cheapest is not None
    assert result.cheapest.price == 1200
    collector._save_all_results.assert_awaited_once()
    saved_results = collector._save_all_results.call_args.args[-1]
    assert [saved.price for saved in saved_results] == [1200]


@pytest.mark.asyncio
async def test_collect_single_date_rejects_wrong_airport_when_leg_route_is_visible() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "scrapingbee"
    provider.search_round_trip_diagnostic = AsyncMock(
        return_value=ProviderSearchOutcome(
            results=[
                make_result(
                    900,
                    airline="Turkish Airlines",
                    provider="scrapingbee",
                    stops=1,
                    raw_data={
                        "airline_names": ["Turkish Airlines"],
                        "legs": [
                            {"airline": "Turkish Airlines", "text": "JFK - MLA"},
                            {"airline": "Turkish Airlines", "text": "MLA - JFK"},
                        ],
                    },
                ),
                make_result(
                    1100,
                    airline="Turkish Airlines",
                    provider="scrapingbee",
                    stops=1,
                    raw_data={
                        "airline_names": ["Turkish Airlines"],
                        "legs": [
                            {"airline": "Turkish Airlines", "text": "EWR - MLA"},
                            {"airline": "Turkish Airlines", "text": "MLA - EWR"},
                        ],
                    },
                ),
            ],
            diagnostics=ProviderSearchDiagnostics(raw_offers_found=2),
        )
    )

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    result = await collector.collect_single_date(
        origin="EWR",
        destination="MLA",
        depart_date=DEPART,
        route_group_id=ROUTE_ID,
        trip_type="round_trip",
        nights=8,
        max_stops=1,
    )

    assert result.cheapest is not None
    assert result.cheapest.price == 1100
    saved_results = collector._save_all_results.call_args.args[-1]
    assert [saved.price for saved in saved_results] == [1100]


@pytest.mark.asyncio
async def test_collect_single_date_clears_stale_rows_when_raw_offers_are_filtered_out() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "scrapingbee"
    provider.search_round_trip_diagnostic = AsyncMock(
        return_value=ProviderSearchOutcome(
            results=[
                make_result(
                    900,
                    airline="Lufthansa",
                    provider="scrapingbee",
                    stops=1,
                    raw_data={
                        "airline_names": ["Lufthansa"],
                        "legs": [
                            {"airline": "Lufthansa", "route_text": "Lufthansa, KM Malta Airlines"},
                            {"airline": "Lufthansa", "route_text": "Lufthansa, KM Malta Airlines"},
                        ],
                    },
                )
            ],
            diagnostics=ProviderSearchDiagnostics(raw_offers_found=1),
        )
    )

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()

    result = await collector.collect_single_date(
        origin="EWR",
        destination="MLA",
        depart_date=DEPART,
        route_group_id=ROUTE_ID,
        trip_type="round_trip",
        nights=8,
        max_stops=1,
    )

    assert result.cheapest is None
    executed_sql = "\n".join(str(call.args[0]) for call in session.execute.await_args_list)
    assert "DELETE FROM daily_cheapest_prices" in executed_sql
    assert "DELETE FROM all_flight_results" in executed_sql


@pytest.mark.asyncio
async def test_collect_single_date_validates_multi_city_open_jaw_legs() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "scrapingbee"
    provider.search_multi_city_diagnostic = AsyncMock(
        return_value=ProviderSearchOutcome(
            results=[
                make_result(
                    800,
                    airline="Turkish Airlines",
                    provider="scrapingbee",
                    stops=1,
                    raw_data={
                        "trip_type": "multi_city",
                        "airline_names": ["Turkish Airlines"],
                        "legs": [
                            {"airline": "Turkish Airlines", "text": "ORD - MLA"},
                            {"airline": "Turkish Airlines", "text": "MLA - JFK"},
                        ],
                    },
                ),
                make_result(
                    950,
                    airline="Turkish Airlines",
                    provider="scrapingbee",
                    stops=1,
                    raw_data={
                        "trip_type": "multi_city",
                        "airline_names": ["Turkish Airlines"],
                        "legs": [
                            {"airline": "Turkish Airlines", "text": "ORD - MLA"},
                            {"airline": "Turkish Airlines", "text": "MLA - ORD"},
                        ],
                    },
                ),
            ],
            diagnostics=ProviderSearchDiagnostics(raw_offers_found=2),
        )
    )

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    result = await collector.collect_single_date(
        origin="ORD",
        destination="MLA",
        depart_date=DEPART,
        route_group_id=ROUTE_ID,
        trip_type="multi_city",
        return_origin="MLA",
        nights=8,
        max_stops=1,
    )

    assert result.cheapest is not None
    assert result.cheapest.price == 950
    saved_results = collector._save_all_results.call_args.args[-1]
    assert [saved.price for saved in saved_results] == [950]


@pytest.mark.asyncio
async def test_collect_single_date_round_trip_forwards_same_airline_only_to_scrapingbee() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "scrapingbee"
    provider.search_round_trip = AsyncMock(
        return_value=[make_result(1800, airline="Air Canada", provider="scrapingbee")]
    )
    provider.search_round_trip_diagnostic = None

    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    await collector.collect_single_date(
        origin="YYZ",
        destination="NRT",
        depart_date=DEPART,
        route_group_id=ROUTE_ID,
        trip_type="round_trip",
        nights=10,
        same_airline_only=True,
    )

    kwargs = provider.search_round_trip.call_args.kwargs
    assert kwargs["same_airline_only"] is True


@pytest.mark.asyncio
async def test_collect_single_date_prefers_shorter_duration_when_price_ties() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider(
        "serpapi",
        [
            make_result(1500, duration_minutes=950, provider="serpapi"),
            make_result(1500, duration_minutes=780, provider="serpapi"),
        ],
    )
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    result = await collector.collect_single_date("YYZ", "NRT", DEPART, ROUTE_ID)

    assert result.cheapest is not None
    assert result.cheapest.price == 1500
    assert result.cheapest.duration_minutes == 780


@pytest.mark.asyncio
async def test_collect_single_date_stop_mode_does_not_hide_cheapest_valid_result() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider(
        "scrapingbee",
        [
            make_result(900, airline="Air Canada", provider="scrapingbee", stops=0),
            make_result(1100, airline="Air Canada", provider="scrapingbee", stops=1),
        ],
    )
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    result = await collector.collect_single_date(
        origin="YYZ",
        destination="EDI",
        depart_date=DEPART,
        route_group_id=ROUTE_ID,
        max_stops=1,
    )

    assert result.cheapest is not None
    assert result.cheapest.price == 900
    assert result.cheapest.stops == 0
