from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.models.route_group import RouteGroup
from app.schemas.route_group import RouteGroupUpdate
from app.services import route_group_service


@pytest.mark.asyncio
async def test_delete_clears_collection_data_before_removing_group() -> None:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()

    group_id = uuid.uuid4()
    group = RouteGroup(
        id=group_id,
        name="Canada to Vietnam",
        destination_label="Vietnam",
        destinations=["SGN", "HAN"],
        origins=["YVR", "YYZ"],
        nights=10,
        days_ahead=90,
        sheet_name_map={"YVR": "Canada", "YYZ": "Canada"},
        special_sheets=[],
        is_active=True,
        market="us",
        currency="USD",
        max_stops=1,
        same_airline_only=False,
        start_date=None,
        end_date=None,
        trip_type="one_way",
        user_id=None,
    )

    with patch.object(route_group_service, "get_by_id", AsyncMock(return_value=group)):
        with patch.object(
            route_group_service,
            "_clear_group_collection_data",
            AsyncMock(),
        ) as clear_mock:
            deleted = await route_group_service.delete(
                session=session,
                group_id=group_id,
            )

    assert deleted is True
    clear_mock.assert_awaited_once_with(session, group_id)
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_clears_collection_data_when_route_identity_changes() -> None:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    group_id = uuid.uuid4()
    group = RouteGroup(
        id=group_id,
        name="Canada to Vietnam",
        destination_label="Vietnam",
        destinations=["SGN", "HAN"],
        origins=["YVR", "YYZ"],
        nights=10,
        days_ahead=90,
        sheet_name_map={"YVR": "Canada", "YYZ": "Canada"},
        special_sheets=[],
        is_active=True,
        market="us",
        currency="USD",
        max_stops=1,
        same_airline_only=False,
        start_date=None,
        end_date=None,
        trip_type="one_way",
        user_id=None,
    )

    with patch.object(route_group_service, "get_by_id", AsyncMock(return_value=group)):
        with patch.object(
            route_group_service,
            "_clear_group_collection_data",
            AsyncMock(),
        ) as clear_mock:
            updated = await route_group_service.update(
                session=session,
                group_id=group_id,
                data=RouteGroupUpdate(destinations=["NRT"]),
            )

    assert updated is group
    assert group.destinations == ["NRT"]
    clear_mock.assert_awaited_once_with(session, group_id)
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once_with(group)


@pytest.mark.asyncio
async def test_update_keeps_collection_data_when_identity_is_unchanged() -> None:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    group_id = uuid.uuid4()
    group = RouteGroup(
        id=group_id,
        name="Canada to Vietnam",
        destination_label="Vietnam",
        destinations=["SGN", "HAN"],
        origins=["YVR", "YYZ"],
        nights=10,
        days_ahead=90,
        sheet_name_map={"YVR": "Canada", "YYZ": "Canada"},
        special_sheets=[],
        is_active=True,
        market="us",
        currency="USD",
        max_stops=1,
        same_airline_only=False,
        start_date=None,
        end_date=None,
        trip_type="one_way",
        user_id=None,
    )

    with patch.object(route_group_service, "get_by_id", AsyncMock(return_value=group)):
        with patch.object(
            route_group_service,
            "_clear_group_collection_data",
            AsyncMock(),
        ) as clear_mock:
            updated = await route_group_service.update(
                session=session,
                group_id=group_id,
                data=RouteGroupUpdate(name="Updated Name"),
            )

    assert updated is group
    assert group.name == "Updated Name"
    clear_mock.assert_not_awaited()
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once_with(group)


@pytest.mark.asyncio
async def test_update_clears_collection_data_when_market_changes() -> None:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    group_id = uuid.uuid4()
    group = RouteGroup(
        id=group_id,
        name="Canada to Vietnam",
        destination_label="Vietnam",
        destinations=["SGN", "HAN"],
        origins=["YVR", "YYZ"],
        nights=10,
        days_ahead=90,
        sheet_name_map={"YVR": "Canada", "YYZ": "Canada"},
        special_sheets=[],
        is_active=True,
        market="us",
        currency="USD",
        max_stops=1,
        same_airline_only=False,
        start_date=None,
        end_date=None,
        trip_type="one_way",
        user_id=None,
    )

    with patch.object(route_group_service, "get_by_id", AsyncMock(return_value=group)):
        with patch.object(
            route_group_service,
            "_clear_group_collection_data",
            AsyncMock(),
        ) as clear_mock:
            updated = await route_group_service.update(
                session=session,
                group_id=group_id,
                data=RouteGroupUpdate(market="ca"),
            )

    assert updated is group
    assert group.market == "ca"
    clear_mock.assert_awaited_once_with(session, group_id)


@pytest.mark.asyncio
async def test_update_keeps_collection_data_when_same_airline_only_changes() -> None:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    group_id = uuid.uuid4()
    group = RouteGroup(
        id=group_id,
        name="Canada to Vietnam",
        destination_label="Vietnam",
        destinations=["SGN", "HAN"],
        origins=["YVR", "YYZ"],
        nights=10,
        days_ahead=90,
        sheet_name_map={"YVR": "Canada", "YYZ": "Canada"},
        special_sheets=[],
        is_active=True,
        market="us",
        currency="USD",
        max_stops=1,
        same_airline_only=False,
        start_date=None,
        end_date=None,
        trip_type="round_trip",
        user_id=None,
    )

    with patch.object(route_group_service, "get_by_id", AsyncMock(return_value=group)):
        with patch.object(
            route_group_service,
            "_clear_group_collection_data",
            AsyncMock(),
        ) as clear_mock:
            updated = await route_group_service.update(
                session=session,
                group_id=group_id,
                data=RouteGroupUpdate(same_airline_only=True),
            )

    assert updated is group
    assert group.same_airline_only is True
    clear_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_keeps_collection_data_when_max_stops_changes() -> None:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    group_id = uuid.uuid4()
    group = RouteGroup(
        id=group_id,
        name="Canada to Vietnam",
        destination_label="Vietnam",
        destinations=["SGN", "HAN"],
        origins=["YVR", "YYZ"],
        nights=10,
        days_ahead=90,
        sheet_name_map={"YVR": "Canada", "YYZ": "Canada"},
        special_sheets=[],
        is_active=True,
        market="us",
        currency="USD",
        max_stops=1,
        same_airline_only=False,
        start_date=None,
        end_date=None,
        trip_type="round_trip",
        user_id=None,
    )

    with patch.object(route_group_service, "get_by_id", AsyncMock(return_value=group)):
        with patch.object(
            route_group_service,
            "_clear_group_collection_data",
            AsyncMock(),
        ) as clear_mock:
            updated = await route_group_service.update(
                session=session,
                group_id=group_id,
                data=RouteGroupUpdate(max_stops=2),
            )

    assert updated is group
    assert group.max_stops == 2
    clear_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_allows_clearing_max_leg_duration() -> None:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    group_id = uuid.uuid4()
    group = RouteGroup(
        id=group_id,
        name="Canada to Vietnam",
        destination_label="Vietnam",
        destinations=["SGN", "HAN"],
        origins=["YVR", "YYZ"],
        nights=10,
        days_ahead=90,
        sheet_name_map={"YVR": "Canada", "YYZ": "Canada"},
        special_sheets=[],
        is_active=True,
        market="us",
        currency="USD",
        max_stops=1,
        same_airline_only=False,
        max_leg_duration_minutes=720,
        start_date=None,
        end_date=None,
        trip_type="round_trip",
        user_id=None,
    )

    with patch.object(route_group_service, "get_by_id", AsyncMock(return_value=group)):
        with patch.object(
            route_group_service,
            "_clear_group_collection_data",
            AsyncMock(),
        ) as clear_mock:
            updated = await route_group_service.update(
                session=session,
                group_id=group_id,
                data=RouteGroupUpdate(max_leg_duration_minutes=None),
            )

    assert updated is group
    assert group.max_leg_duration_minutes is None
    clear_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_resets_group_safeguard_state_on_manual_resume() -> None:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    group_id = uuid.uuid4()
    group = RouteGroup(
        id=group_id,
        name="Canada to Vietnam",
        destination_label="Vietnam",
        destinations=["SGN", "HAN"],
        origins=["YVR", "YYZ"],
        nights=10,
        days_ahead=90,
        sheet_name_map={"YVR": "Canada", "YYZ": "Canada"},
        special_sheets=[],
        is_active=False,
        market="us",
        currency="USD",
        max_stops=1,
        same_airline_only=False,
        max_leg_duration_minutes=720,
        consecutive_operational_failures=2,
        last_operational_failure_at=None,
        last_auto_pause_reason="repeated_operational_failures",
        last_auto_pause_note="Paused after repeated failures.",
        deferred_retry_state=[
            {
                "origin": "YVR",
                "destinations": ["SGN"],
                "trip_type": "round_trip",
                "depart_date": "2026-05-30",
                "mode": "operational_retry",
            }
        ],
        start_date=None,
        end_date=None,
        trip_type="round_trip",
        user_id=None,
    )

    with patch.object(route_group_service, "get_by_id", AsyncMock(return_value=group)):
        updated = await route_group_service.update(
            session=session,
            group_id=group_id,
            data=RouteGroupUpdate(is_active=True),
        )

    assert updated is group
    assert group.is_active is True
    assert group.consecutive_operational_failures == 0
    assert group.last_operational_failure_at is None
    assert group.last_auto_pause_reason is None
    assert group.last_auto_pause_note is None
    assert group.deferred_retry_state == []
