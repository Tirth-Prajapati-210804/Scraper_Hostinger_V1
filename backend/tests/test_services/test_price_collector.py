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
    # The stored/surfaced error is now the SHORT friendly message, not the raw
    # provider text (which can be a long ScrapingBee blob). Status still classifies
    # it as parse_error.
    assert result.errors == {"searchapi": "Could not read the rendered page - will retry."}
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
async def test_round_trip_batch_uses_one_combined_multi_airport_search() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = make_provider("searchapi", [make_result(205, provider="searchapi")])
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._upsert_cheapest = AsyncMock()
    collector._save_all_results = AsyncMock()

    stats = await collector.collect_route_batch(
        origin="GLA,PIK",
        destinations=["MLA,CTA"],
        dates=[DEPART],
        route_group_id=ROUTE_ID,
        batch_size=2,
        delay_seconds=0,
        trip_type="round_trip",
        nights=7,
    )

    assert stats == {"success": 1, "errors": 0, "skipped": 0}
    provider.search_round_trip.assert_awaited_once()
    assert provider.search_round_trip.await_args.kwargs["origin"] == "GLA,PIK"
    assert provider.search_round_trip.await_args.kwargs["destination"] == "MLA,CTA"
    collector._upsert_cheapest.assert_awaited_once()
    assert collector._upsert_cheapest.await_args.args[2] == "GLA,PIK"
    assert collector._upsert_cheapest.await_args.args[3] == "MLA,CTA"


@pytest.mark.asyncio
async def test_multi_city_batch_compares_destination_alternatives_before_saving() -> None:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "searchapi"
    provider.search_multi_city_diagnostic = None

    async def search_multi_city(**kwargs):
        first_leg = kwargs["legs"][0]
        destination = first_leg["arrival_id"]
        if destination == "BER":
            return [make_result(900, provider="searchapi", raw_data={"trip_type": "multi_city"})]
        if destination == "BUD":
            return [make_result(700, provider="searchapi", raw_data={"trip_type": "multi_city"})]
        return []

    provider.search_multi_city = AsyncMock(side_effect=search_multi_city)
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._save_all_results = AsyncMock()
    collector._delete_daily_cheapest_for_destinations = AsyncMock()
    collector._upsert_cheapest = AsyncMock()

    stats = await collector.collect_route_batch(
        origin="YYZ",
        destinations=["BER", "BUD"],
        dates=[DEPART],
        route_group_id=ROUTE_ID,
        batch_size=2,
        delay_seconds=0,
        trip_type="multi_city",
        extra_legs=[],
        return_origin="YYZ",
        compare_destinations=True,
    )

    assert stats == {"success": 2, "errors": 0, "skipped": 0}
    assert provider.search_multi_city.await_count == 2
    # ONLY the winning variant's offers are archived (a losing variant writing
    # all_flight_results made the table/export show the loser's airports next to
    # the winner's price/link).
    assert collector._save_all_results.await_count == 1
    assert collector._save_all_results.await_args.args[3] == "BUD"
    collector._delete_daily_cheapest_for_destinations.assert_awaited_once()
    collector._upsert_cheapest.assert_awaited_once()
    assert collector._upsert_cheapest.await_args.kwargs["destination"] == "BUD"
    assert collector._upsert_cheapest.await_args.kwargs["result"].price == 700


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
    # Return date = depart + nights (the +1 was removed per the client's request).
    assert kwargs["return_date"] == DEPART + timedelta(days=10)
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


@pytest.mark.asyncio
async def test_multi_city_batch_compares_leg_airport_alternatives() -> None:
    """A leg's comma alternatives ("ASJ,SES") expand into separate searches per
    date; only the cheapest chain is saved (winner under the leg-1 destination)."""
    from app.utils.route_segments import ExtraLeg

    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "searchapi"
    provider.search_multi_city_diagnostic = None

    async def search_multi_city(**kwargs):
        leg2_origin = kwargs["legs"][1]["departure_id"]
        if leg2_origin == "ASJ":
            return [make_result(880, provider="searchapi", raw_data={"trip_type": "multi_city"})]
        if leg2_origin == "SES":
            return [make_result(640, provider="searchapi", raw_data={"trip_type": "multi_city"})]
        return []

    provider.search_multi_city = AsyncMock(side_effect=search_multi_city)
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._save_all_results = AsyncMock()
    collector._delete_daily_cheapest_for_destinations = AsyncMock()
    collector._upsert_cheapest = AsyncMock()

    stats = await collector.collect_route_batch(
        origin="YYZ",
        destinations=["ICN"],
        dates=[DEPART],
        route_group_id=ROUTE_ID,
        batch_size=2,
        delay_seconds=0,
        trip_type="multi_city",
        extra_legs=[ExtraLeg(origin="ASJ,SES", destination="", nights_before=5)],
        return_origin="YYZ",
        compare_destinations=True,
    )

    assert stats == {"success": 2, "errors": 0, "skipped": 0}
    assert provider.search_multi_city.await_count == 2
    # Winner-only archive: one save, under the leg-1 destination, and only the
    # winning chain's offers (the SES variant's 640 fare).
    assert collector._save_all_results.await_count == 1
    assert collector._save_all_results.await_args.args[3] == "ICN"
    saved_offers = collector._save_all_results.await_args.args[5]
    assert [offer.price for offer in saved_offers] == [640]
    collector._upsert_cheapest.assert_awaited_once()
    assert collector._upsert_cheapest.await_args.kwargs["destination"] == "ICN"
    assert collector._upsert_cheapest.await_args.kwargs["result"].price == 640


@pytest.mark.asyncio
async def test_multi_city_batch_compares_origin_and_destination_alternatives() -> None:
    """Multiple first-leg origins and destinations expand to concrete searches;
    only the cheapest full itinerary is stored under the combined origin key."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "searchapi"
    provider.search_multi_city_diagnostic = None

    prices_by_route = {
        ("GLA", "MLA"): 420,
        ("GLA", "CTA"): 390,
        ("PIK", "MLA"): 310,
        ("PIK", "CTA"): 220,
    }

    async def search_multi_city(**kwargs):
        first_leg = kwargs["legs"][0]
        route = (first_leg["departure_id"], first_leg["arrival_id"])
        price = prices_by_route.get(route)
        if price is None:
            return []
        return [make_result(price, provider="searchapi", raw_data={"trip_type": "multi_city"})]

    provider.search_multi_city = AsyncMock(side_effect=search_multi_city)
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._save_all_results = AsyncMock()
    collector._delete_daily_cheapest_for_destinations = AsyncMock()
    collector._delete_all_flight_results_for_destinations = AsyncMock()
    collector._upsert_cheapest = AsyncMock()

    stats = await collector.collect_route_batch(
        origin="GLA,PIK",
        destinations=["MLA", "CTA"],
        dates=[DEPART],
        route_group_id=ROUTE_ID,
        batch_size=4,
        delay_seconds=0,
        trip_type="multi_city",
        extra_legs=[],
        return_origin="MLA",
        compare_destinations=True,
    )

    searched_routes = {
        (
            call.kwargs["legs"][0]["departure_id"],
            call.kwargs["legs"][0]["arrival_id"],
        )
        for call in provider.search_multi_city.await_args_list
    }

    assert stats == {"success": 4, "errors": 0, "skipped": 0}
    assert searched_routes == {
        ("GLA", "MLA"),
        ("GLA", "CTA"),
        ("PIK", "MLA"),
        ("PIK", "CTA"),
    }
    assert collector._save_all_results.await_count == 1
    assert collector._save_all_results.await_args.args[2] == "GLA,PIK"
    assert collector._save_all_results.await_args.args[3] == "CTA"
    collector._delete_daily_cheapest_for_destinations.assert_awaited_once()
    collector._delete_all_flight_results_for_destinations.assert_awaited_once()
    collector._upsert_cheapest.assert_awaited_once()
    assert collector._upsert_cheapest.await_args.kwargs["origin"] == "GLA,PIK"
    assert collector._upsert_cheapest.await_args.kwargs["destination"] == "CTA"
    assert collector._upsert_cheapest.await_args.kwargs["result"].price == 220


@pytest.mark.asyncio
async def test_multi_city_compare_saves_cheapest_variant_not_last_variant() -> None:
    """Comparison mode must pick the cheapest offer across variants and archive
    only that winner's offers, even when a later searched variant has data."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "searchapi"
    provider.search_multi_city_diagnostic = None

    async def search_multi_city(**kwargs):
        first_origin = kwargs["legs"][0]["departure_id"]
        if first_origin == "GLA":
            return [
                make_result(900, provider="searchapi", raw_data={"variant": "GLA"}),
                make_result(500, provider="searchapi", raw_data={"variant": "GLA"}),
            ]
        if first_origin == "PIK":
            return [
                make_result(700, provider="searchapi", raw_data={"variant": "PIK"}),
                make_result(650, provider="searchapi", raw_data={"variant": "PIK"}),
            ]
        return []

    provider.search_multi_city = AsyncMock(side_effect=search_multi_city)
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._save_all_results = AsyncMock()
    collector._delete_daily_cheapest_for_destinations = AsyncMock()
    collector._delete_all_flight_results_for_destinations = AsyncMock()
    collector._upsert_cheapest = AsyncMock()

    stats = await collector.collect_route_batch(
        origin="GLA,PIK",
        destinations=["KEF"],
        dates=[DEPART],
        route_group_id=ROUTE_ID,
        batch_size=1,
        delay_seconds=0,
        trip_type="multi_city",
        extra_legs=[],
        return_origin="KEF",
        compare_destinations=True,
    )

    assert stats == {"success": 2, "errors": 0, "skipped": 0}
    assert [
        call.kwargs["legs"][0]["departure_id"]
        for call in provider.search_multi_city.await_args_list
    ] == ["GLA", "PIK"]
    collector._upsert_cheapest.assert_awaited_once()
    assert collector._upsert_cheapest.await_args.kwargs["origin"] == "GLA,PIK"
    assert collector._upsert_cheapest.await_args.kwargs["destination"] == "KEF"
    assert collector._upsert_cheapest.await_args.kwargs["result"].price == 500
    saved_offers = collector._save_all_results.await_args.args[5]
    assert [offer.price for offer in saved_offers] == [900, 500]
    assert {offer.raw_data["variant"] for offer in saved_offers} == {"GLA"}


@pytest.mark.asyncio
async def test_multi_city_batch_compares_four_leg_origin_alternatives_end_to_end() -> None:
    """A 4-leg multi-city chain searches concrete origins but stores one winner
    under the combined origin; the empty final destination returns to that
    concrete origin variant, not the comma key."""
    from app.utils.route_segments import ExtraLeg

    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()

    provider = MagicMock()
    provider.name = "searchapi"
    provider.search_multi_city_diagnostic = None

    seen_leg_chains: list[list[tuple[str, str]]] = []

    async def search_multi_city(**kwargs):
        chain = [
            (leg["departure_id"], leg["arrival_id"])
            for leg in kwargs["legs"]
        ]
        seen_leg_chains.append(chain)
        price = 530 if chain[0][0] == "GLA" else 410
        return [make_result(price, provider="searchapi", raw_data={"trip_type": "multi_city"})]

    provider.search_multi_city = AsyncMock(side_effect=search_multi_city)
    collector = PriceCollector(
        session_factory=make_session_factory(session),
        providers=[provider],
    )
    collector._save_all_results = AsyncMock()
    collector._delete_daily_cheapest_for_destinations = AsyncMock()
    collector._delete_all_flight_results_for_destinations = AsyncMock()
    collector._upsert_cheapest = AsyncMock()

    stats = await collector.collect_route_batch(
        origin="GLA,PIK",
        destinations=["KEF"],
        dates=[DEPART],
        route_group_id=ROUTE_ID,
        batch_size=2,
        delay_seconds=0,
        trip_type="multi_city",
        extra_legs=[
            ExtraLeg(origin="KEF", destination="YYZ", nights_before=2),
            ExtraLeg(origin="NYC", destination="BOS", nights_before=5),
            ExtraLeg(origin="BOS", destination="", nights_before=3),
        ],
        compare_destinations=True,
    )

    assert stats == {"success": 2, "errors": 0, "skipped": 0}
    assert seen_leg_chains == [
        [("GLA", "KEF"), ("KEF", "YYZ"), ("NYC", "BOS"), ("BOS", "GLA")],
        [("PIK", "KEF"), ("KEF", "YYZ"), ("NYC", "BOS"), ("BOS", "PIK")],
    ]
    collector._save_all_results.assert_awaited_once()
    assert collector._save_all_results.await_args.args[2] == "GLA,PIK"
    collector._upsert_cheapest.assert_awaited_once()
    assert collector._upsert_cheapest.await_args.kwargs["origin"] == "GLA,PIK"
    assert collector._upsert_cheapest.await_args.kwargs["result"].price == 410
