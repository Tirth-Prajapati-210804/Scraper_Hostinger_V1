from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redaction import redact_text
from app.models.collection_run import CollectionRun
from app.models.route_group import RouteGroup
from app.providers.registry import ProviderRegistry
from app.services.alert_service import AlertService
from app.services.price_collector import PriceCollector
from app.utils.route_segments import iter_group_segments

log = get_logger(__name__)


class FlightScheduler:
    """
    Goal B Final:
    - freshness scheduling
    - historical route scoring
    - smart collection ordering
    - lower quota waste
    """

    _MAX_DATES = 730
    _RUN_CONTEXT_CODE = "run_context"

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        provider_registry: ProviderRegistry,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.provider_registry = provider_registry

        self.alert_service = AlertService(settings)
        self.scheduler = AsyncIOScheduler(timezone="UTC")

        self._is_running = False
        self._is_collecting = False
        self._stop_requested = False
        self._lock_connection: AsyncConnection | None = None
        self._active_task: asyncio.Task | None = None
        self._check_states: dict[tuple[str, str, str], str] = {}
        self._active_checks: set[tuple[str, str, str]] = set()
        self._retry_started = 0
        self._retry_done = 0
        self._planned_checks_total = 0

        self._progress: dict = {
            "routes_total": 0,
            "routes_done": 0,
            "routes_failed": 0,
            "checks_total": 0,
            "checks_started": 0,
            "checks_done": 0,
            "checks_failed": 0,
            "active_searches": 0,
            "retries_started": 0,
            "retries_done": 0,
            "prices_total": 0,
            "prices_started": 0,
            "prices_done": 0,
            "prices_failed": 0,
            "dates_scraped": 0,
            "current_origin": "",
            "current_destination": "",
            "current_date": "",
        }

    # --------------------------------------------------
    # STATE
    # --------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._is_running and self.scheduler.running

    @property
    def is_collecting(self) -> bool:
        return self._is_collecting

    @property
    def progress(self) -> dict:
        return dict(self._progress)

    def _reset_progress(self) -> None:
        self._check_states = {}
        self._active_checks = set()
        self._retry_started = 0
        self._retry_done = 0
        self._planned_checks_total = 0
        self._progress = {
            "routes_total": 0,
            "routes_done": 0,
            "routes_failed": 0,
            "checks_total": 0,
            "checks_started": 0,
            "checks_done": 0,
            "checks_failed": 0,
            "active_searches": 0,
            "retries_started": 0,
            "retries_done": 0,
            "prices_total": 0,
            "prices_started": 0,
            "prices_done": 0,
            "prices_failed": 0,
            "dates_scraped": 0,
            "current_origin": "",
            "current_destination": "",
            "current_date": "",
        }

    def _check_key(self, origin: str, destination: str, depart_date: date) -> tuple[str, str, str]:
        return (origin, destination, depart_date.isoformat())

    def _sync_progress(self) -> None:
        checks_started = len(self._check_states)
        checks_done = sum(
            1 for status in self._check_states.values() if status in {"success", "skipped", "error"}
        )
        checks_failed = sum(1 for status in self._check_states.values() if status == "error")

        self._progress["checks_total"] = self._planned_checks_total
        self._progress["checks_started"] = checks_started
        self._progress["checks_done"] = checks_done
        self._progress["checks_failed"] = checks_failed
        self._progress["active_searches"] = len(self._active_checks)
        self._progress["retries_started"] = self._retry_started
        self._progress["retries_done"] = self._retry_done

        # Preserve the old shape as aliases while the frontend transitions.
        self._progress["prices_total"] = self._planned_checks_total
        self._progress["prices_started"] = checks_started + self._retry_started
        self._progress["prices_done"] = checks_done
        self._progress["prices_failed"] = checks_failed

    def _record_item_started(
        self,
        origin: str,
        destination: str,
        depart_date: date,
        is_retry: bool,
    ) -> None:
        key = self._check_key(origin, destination, depart_date)
        self._progress["current_origin"] = origin
        self._progress["current_destination"] = destination
        self._progress["current_date"] = depart_date.isoformat()
        self._active_checks.add(key)
        if is_retry:
            self._retry_started += 1
        else:
            self._check_states.setdefault(key, "started")
        self._sync_progress()

    def _record_item_progress(
        self,
        status: str,
        origin: str,
        destination: str,
        depart_date: date,
        is_retry: bool,
    ) -> None:
        key = self._check_key(origin, destination, depart_date)
        self._progress["current_origin"] = origin
        self._progress["current_destination"] = destination
        self._progress["current_date"] = depart_date.isoformat()
        self._active_checks.discard(key)

        if status == "stopped":
            if is_retry:
                self._retry_done += 1
            self._sync_progress()
            return

        if status == "success":
            self._progress["dates_scraped"] += 1
            self._check_states[key] = "success"
        elif status == "error":
            self._check_states[key] = "error"
        else:
            self._check_states.setdefault(key, "skipped")

        if is_retry:
            self._retry_done += 1
        elif key not in self._check_states:
            self._check_states[key] = status

        self._sync_progress()

    def request_stop(self) -> None:
        self._stop_requested = True

    def _run_context_payload(
        self,
        *,
        mode: str,
        group_id: UUID | None = None,
        target_dates: list[date] | None = None,
    ) -> list[dict[str, object]]:
        payload: dict[str, object] = {
            "code": self._RUN_CONTEXT_CODE,
            "mode": mode,
        }
        if group_id is not None:
            payload["group_id"] = str(group_id)
        if target_dates:
            payload["target_dates"] = [d.isoformat() for d in target_dates]
        return [payload]

    def _resume_context_from_run(
        self,
        run: CollectionRun,
    ) -> tuple[str, UUID | None, list[date] | None]:
        entries = run.errors if isinstance(run.errors, list) else []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("code") != self._RUN_CONTEXT_CODE:
                continue
            mode = str(entry.get("mode") or "all")
            parsed_group_id: UUID | None = None
            raw_group_id = entry.get("group_id")
            if isinstance(raw_group_id, str):
                try:
                    parsed_group_id = UUID(raw_group_id)
                except ValueError:
                    parsed_group_id = None
            parsed_dates: list[date] | None = None
            raw_target_dates = entry.get("target_dates")
            if isinstance(raw_target_dates, list):
                parsed_dates = []
                for raw_date in raw_target_dates:
                    if not isinstance(raw_date, str):
                        continue
                    try:
                        parsed_dates.append(date.fromisoformat(raw_date))
                    except ValueError:
                        continue
            return mode, parsed_group_id, parsed_dates or None
        return "all", None, None

    async def recover_incomplete_run(self) -> bool:
        if self._active_task is not None and not self._active_task.done():
            return False

        async with self.session_factory() as session:
            result = await session.execute(
                select(CollectionRun)
                .where(CollectionRun.status == "running")
                .order_by(CollectionRun.started_at.desc(), CollectionRun.created_at.desc())
            )
            stale_runs = list(result.scalars().all())
            if not stale_runs:
                return False

            newest_run = stale_runs[0]
            mode, group_id, target_dates = self._resume_context_from_run(newest_run)
            finished_at = datetime.now(UTC)

            for run in stale_runs:
                run.status = "failed"
                run.finished_at = finished_at
                if run.id == newest_run.id:
                    run.errors = [
                        {
                            "code": "restarted_mid_collection",
                            "detail": "Server restarted mid-collection. Automatic recovery started.",
                        }
                    ]
                else:
                    run.errors = [
                        {
                            "code": "superseded_by_recovery",
                            "detail": "Automatic recovery resumed a newer interrupted collection run.",
                        }
                    ]

            await session.commit()

        if mode == "single_group" and group_id is not None:
            started = self.start_single_group_task(group_id, target_dates)
        else:
            started = self.start_collection_task()

        log.info(
            "collection_recovery_started",
            started=started,
            mode=mode,
            group_id=str(group_id) if group_id else None,
            target_dates=[d.isoformat() for d in target_dates] if target_dates else None,
        )
        return started

    # --------------------------------------------------
    # START
    # --------------------------------------------------

    def start(self) -> None:
        if not self.settings.scheduler_enabled:
            return

        self.scheduler.add_job(
            self.run_collection_cycle,
            trigger="interval",
            minutes=self.settings.scheduler_interval_minutes,
            id="flight_collection",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

        self.scheduler.add_job(
            self.cleanup_old_data,
            trigger="interval",
            hours=24,
            id="daily_cleanup",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

        self.scheduler.start()
        self._is_running = True

        log.info(
            "scheduler_started",
            interval=self.settings.scheduler_interval_minutes,
        )

    async def stop(self) -> None:
        self.request_stop()
        if self._active_task is not None and not self._active_task.done():
            try:
                await asyncio.wait_for(self._active_task, timeout=5)
            except TimeoutError:
                self._active_task.cancel()
            except Exception:
                pass

        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

        if self._lock_connection is not None:
            try:
                await self._lock_connection.close()
            finally:
                self._lock_connection = None

        self._is_running = False

    def _track_task(self, task: asyncio.Task) -> None:
        self._active_task = task

        def _cleanup(done_task: asyncio.Task) -> None:
            if self._active_task is done_task:
                self._active_task = None
            try:
                done_task.result()
            except asyncio.CancelledError:
                log.info("collection_task_cancelled")
            except Exception as exc:
                log.exception(
                    "collection_task_failed",
                    error=redact_text(str(exc)),
                )

        task.add_done_callback(_cleanup)

    def start_collection_task(self) -> bool:
        if self._active_task is not None and not self._active_task.done():
            return False

        task = asyncio.create_task(self.run_collection_cycle())
        self._track_task(task)
        return True

    def start_single_group_task(
        self,
        group_id: UUID,
        target_dates: list[date] | None = None,
    ) -> bool:
        if self._active_task is not None and not self._active_task.done():
            return False

        task = asyncio.create_task(self.trigger_single_group(group_id, target_dates))
        self._track_task(task)
        return True

    def _route_parallelism(self, route_count: int) -> int:
        try:
            configured = int(getattr(self.settings, "scrape_route_parallelism", 1) or 1)
        except (TypeError, ValueError):
            configured = 1
        return max(1, min(configured, max(route_count, 1)))

    async def _collect_segment_with_retry(
        self,
        *,
        collector: PriceCollector,
        group: RouteGroup,
        segment: object,
        remaining: list[date],
    ) -> dict[str, int]:
        stats = {
            "success": 0,
            "errors": 0,
            "skipped": 0,
            "final_missing": 0,
        }

        if self._stop_requested:
            return stats

        self._progress["current_origin"] = segment.origin
        self._progress["current_destination"] = ""
        self._progress["current_date"] = ""

        part = await collector.collect_route_batch(
            origin=segment.origin,
            destinations=segment.destinations,
            dates=remaining,
            route_group_id=group.id,
            batch_size=self.settings.scrape_batch_size,
            delay_seconds=self.settings.scrape_delay_seconds,
            stop_check=lambda: self._stop_requested,
            market=getattr(group, "market", None),
            currency=group.currency,
            max_stops=group.max_stops,
            same_airline_only=getattr(group, "same_airline_only", False),
            max_leg_duration_minutes=getattr(group, "max_leg_duration_minutes", None),
            trip_type=segment.trip_type,
            nights=segment.nights,
            return_origin=segment.return_origin,
        )

        stats["success"] += part["success"]
        stats["errors"] += part["errors"]
        stats["skipped"] += part["skipped"]

        if self._stop_requested:
            return stats

        async with self.session_factory() as check_session:
            missing = await self._filter_already_scraped(
                session=check_session,
                route_group_id=group.id,
                origin=segment.origin,
                destinations=segment.destinations,
                dates=remaining,
            )

        if missing:
            retry = await collector.collect_route_batch(
                origin=segment.origin,
                destinations=segment.destinations,
                dates=missing,
                route_group_id=group.id,
                batch_size=self.settings.scrape_batch_size,
                delay_seconds=self.settings.scrape_delay_seconds,
                stop_check=lambda: self._stop_requested,
                market=getattr(group, "market", None),
                currency=group.currency,
                max_stops=group.max_stops,
                same_airline_only=getattr(group, "same_airline_only", False),
                max_leg_duration_minutes=getattr(group, "max_leg_duration_minutes", None),
                trip_type=segment.trip_type,
                nights=segment.nights,
                return_origin=segment.return_origin,
                is_retry=True,
            )
            stats["success"] += retry["success"]
            stats["errors"] += retry["errors"]
            stats["skipped"] += retry["skipped"]

            if not self._stop_requested:
                async with self.session_factory() as check_session:
                    missing = await self._filter_already_scraped(
                        session=check_session,
                        route_group_id=group.id,
                        origin=segment.origin,
                        destinations=segment.destinations,
                        dates=missing,
                    )

        stats["final_missing"] = len(missing) * len(segment.destinations) if not self._stop_requested else 0
        return stats

    # --------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------

    async def run_collection_cycle(self) -> None:
        if self._is_collecting:
            return

        self._is_collecting = True
        self._stop_requested = False

        lock_acquired = False

        try:
            async with self.session_factory() as session:
                lock_acquired = await self._acquire_global_lock(session)
                if not lock_acquired:
                    log.warning("collection_lock_unavailable", mode="all")
                    return

                try:
                    run = CollectionRun(
                        status="running",
                        started_at=datetime.now(UTC),
                        errors=self._run_context_payload(mode="all"),
                    )
                    session.add(run)
                    await session.flush()
                    await session.commit()

                    providers = self.provider_registry.get_enabled()

                    if not providers:
                        run.status = "failed"
                        run.errors = [
                            {
                                "code": "provider_unavailable",
                                "detail": "No enabled flight data provider is available for collection.",
                                "provider_status": self.provider_registry.status(),
                            }
                        ]
                        run.finished_at = func.now()
                        await session.commit()
                        return

                    groups_result = await session.execute(
                        select(RouteGroup).where(RouteGroup.is_active.is_(True))
                    )
                    groups = list(groups_result.scalars().all())

                    ranked_routes = []

                    for group in groups:
                        dates = self._group_dates(group)

                        for segment in iter_group_segments(group):
                            score = await self._route_score(
                                session=session,
                                group_id=group.id,
                                origin=segment.origin,
                            )

                            ranked_routes.append(
                                (
                                    score,
                                    group,
                                    segment,
                                    dates,
                                )
                            )

                    ranked_routes.sort(key=lambda x: x[0], reverse=True)

                    total_success = 0
                    total_errors = 0
                    total_skipped = 0
                    route_success = 0
                    route_failed = 0
                    planned_routes: list[tuple[RouteGroup, object, list[date]]] = []
                    self._reset_progress()

                    for _, group, segment, dates in ranked_routes:
                        if self._stop_requested:
                            break

                        remaining = await self._filter_already_scraped(
                            session=session,
                            route_group_id=group.id,
                            origin=segment.origin,
                            destinations=segment.destinations,
                            dates=dates,
                        )

                        if not remaining:
                            continue

                        planned_routes.append((group, segment, remaining))
                        self._planned_checks_total += len(remaining) * len(segment.destinations)

                    self._sync_progress()
                    self._progress["routes_total"] = len(planned_routes)
                    run.routes_total = len(planned_routes)
                    await session.commit()

                    collector = PriceCollector(
                        session_factory=self.session_factory,
                        providers=providers,
                        on_provider_success=self.provider_registry.report_success,
                        on_provider_failure=self.provider_registry.report_failure,
                        on_item_started=lambda origin, destination, depart_date, is_retry: self._record_item_started(
                            origin,
                            destination,
                            depart_date,
                            is_retry,
                        ),
                        on_item_progress=lambda status, origin, destination, depart_date, is_retry: self._record_item_progress(
                            status,
                            origin,
                            destination,
                            depart_date,
                            is_retry,
                        ),
                    )

                    route_semaphore = asyncio.Semaphore(self._route_parallelism(len(planned_routes)))

                    async def collect_planned_route(group: RouteGroup, segment: object, remaining: list[date]):
                        async with route_semaphore:
                            if self._stop_requested:
                                return {
                                    "success": 0,
                                    "errors": 0,
                                    "skipped": len(remaining) * len(segment.destinations),
                                    "final_missing": 0,
                                }
                            try:
                                return await self._collect_segment_with_retry(
                                    collector=collector,
                                    group=group,
                                    segment=segment,
                                    remaining=remaining,
                                )
                            except Exception as exc:
                                log.exception(
                                    "route_failed",
                                    origin=segment.origin,
                                    error=redact_text(str(exc)),
                                )
                                return {
                                    "success": 0,
                                    "errors": 1,
                                    "skipped": 0,
                                    "final_missing": len(remaining) * len(segment.destinations),
                                }
                            finally:
                                self._progress["routes_done"] += 1
                                self._sync_progress()

                    route_results = await asyncio.gather(
                        *(
                            collect_planned_route(group, segment, remaining)
                            for group, segment, remaining in planned_routes
                        )
                    )

                    total_final_missing = 0
                    for stats in route_results:
                        try:
                            total_success += stats["success"]
                            total_errors += stats["errors"]
                            total_skipped += stats["skipped"]
                            total_final_missing += stats["final_missing"]
                            if stats["success"] > 0 and stats["final_missing"] == 0:
                                route_success += 1
                            if stats["errors"] > 0 or stats["final_missing"] > 0:
                                route_failed += 1

                        except Exception as exc:
                            total_errors += 1
                            route_failed += 1
                            self._progress["routes_failed"] += 1

                            log.exception(
                                "route_result_failed",
                                error=redact_text(str(exc)),
                            )

                    if self._stop_requested:
                        run.status = "stopped"
                        run.errors = []
                    elif total_success == 0 and total_errors > 0:
                        run.status = "failed"
                    elif total_final_missing > 0:
                        run.status = "partial"
                        run.errors = [
                            {
                                    "code": "missing_fares",
                                    "detail": (
                                        f"{total_final_missing} date/destination check(s) returned "
                                        "no valid fare after filtering and still need collection."
                                    ),
                            }
                        ]
                    else:
                        run.status = "completed"
                        run.errors = []
                    run.routes_total = len(planned_routes)
                    run.routes_success = route_success
                    run.routes_failed = route_failed
                    run.dates_scraped = total_success
                    run.finished_at = datetime.now(UTC)
                    await session.commit()

                finally:
                    if lock_acquired:
                        try:
                            await self._release_global_lock(session)
                        except Exception:
                            pass

        finally:
            self._is_collecting = False
            self._stop_requested = False

    # --------------------------------------------------
    # HISTORICAL ROUTE SCORE
    # --------------------------------------------------

    async def _route_score(
        self,
        session,
        group_id,
        origin,
    ) -> float:

        result = await session.execute(
            text(
                """
                SELECT
                    COALESCE(MIN(price), 999999),
                    COUNT(*)
                FROM daily_cheapest_prices
                WHERE route_group_id = :gid
                  AND origin = :origin
                  AND depart_date >= current_date
                """
            ),
            {
                "gid": str(group_id),
                "origin": origin,
            },
        )

        row = result.first()

        min_price = float(row[0] or 999999)
        volume = int(row[1] or 0)

        price_score = max(0, 5000 - min_price)
        volume_score = min(volume * 5, 500)

        return price_score + volume_score

    # --------------------------------------------------
    # DATES
    # --------------------------------------------------

    def _group_dates(self, group: RouteGroup) -> list[date]:
        today = date.today()

        configured_start = group.start_date or today
        start = max(configured_start, today)
        date_count = max(1, min(group.days_ahead, self._MAX_DATES))
        end = group.end_date or (start + timedelta(days=date_count - 1))
        if end < start:
            return []

        total_days = min(
            (end - start).days + 1,
            self._MAX_DATES,
        )

        return [
            start + timedelta(days=i)
            for i in range(total_days)
        ]

    # --------------------------------------------------
    # COMPLETION FILTER
    # --------------------------------------------------

    async def _filter_already_scraped(
        self,
        session,
        route_group_id,
        origin,
        destinations,
        dates,
    ):
        """Return dates that still need work (not all destinations collected).

        One grouped query keeps this O(1) round-trips per group instead of
        O(dates), which mattered as days_ahead grew toward 365+.
        """

        if not dates or not destinations:
            return list(dates)

        # query. Under-scraping is the costly failure mode — extra scrapes are

        result = await session.execute(
            text(
                """
                SELECT depart_date, COUNT(DISTINCT destination)
                FROM daily_cheapest_prices
                WHERE route_group_id = :route_group_id
                AND origin = :origin
                AND destination = ANY(:destinations)
                AND depart_date = ANY(:dates)
                GROUP BY depart_date
                """
            ),
            {
                "route_group_id": str(route_group_id),
                "origin": origin,
                "destinations": list(destinations),
                "dates": list(dates),
            },
        )

        done_by_date = {row[0]: row[1] for row in result.fetchall()}
        target = len(destinations)

        return [d for d in dates if done_by_date.get(d, 0) < target]

    # --------------------------------------------------
    # LOCKS
    # --------------------------------------------------

    async def _acquire_global_lock(self, session) -> bool:
        bind = getattr(self.session_factory, "kw", {}).get("bind")
        if isinstance(bind, AsyncEngine):
            connection = await bind.connect()
            try:
                result = await connection.execute(
                    text("SELECT pg_try_advisory_lock(987654321)")
                )
                locked = bool(result.scalar())
                if locked:
                    self._lock_connection = connection
                    return True
            except Exception:
                await connection.close()
                raise

            await connection.close()
            return False

        result = await session.execute(
            text("SELECT pg_try_advisory_lock(987654321)")
        )
        return bool(result.scalar())

    async def _release_global_lock(self, session):
        if self._lock_connection is not None:
            try:
                await self._lock_connection.execute(
                    text("SELECT pg_advisory_unlock(987654321)")
                )
            finally:
                await self._lock_connection.close()
                self._lock_connection = None
            return

        await session.execute(
            text("SELECT pg_advisory_unlock(987654321)")
        )

    # --------------------------------------------------
    # MANUAL
    # --------------------------------------------------

    async def trigger_single_group(
        self,
        group_id: UUID,
        target_dates: list[date] | None = None,
    ) -> dict[str, int]:

        stats = {
            "success": 0,
            "errors": 0,
            "skipped": 0,
        }

        if self._is_collecting:
            return stats

        self._is_collecting = True
        self._stop_requested = False
        lock_acquired = False

        try:
            async with self.session_factory() as session:
                lock_acquired = await self._acquire_global_lock(session)
                if not lock_acquired:
                    log.warning("collection_lock_unavailable", mode="single_group", group_id=str(group_id))
                    return stats

                try:
                    run = CollectionRun(
                        status="running",
                        started_at=datetime.now(UTC),
                        errors=self._run_context_payload(
                            mode="single_group",
                            group_id=group_id,
                            target_dates=target_dates,
                        ),
                    )
                    session.add(run)
                    await session.flush()
                    await session.commit()

                    result = await session.execute(
                        select(RouteGroup).where(
                            RouteGroup.id == group_id,
                            RouteGroup.is_active.is_(True),
                        )
                    )

                    group = result.scalar_one_or_none()

                    if not group:
                        run.status = "failed"
                        run.errors = [{"code": "group_not_found", "detail": "Route group not found or inactive."}]
                        run.finished_at = datetime.now(UTC)
                        await session.commit()
                        return stats

                    providers = self.provider_registry.get_enabled()

                    if not providers:
                        run.status = "failed"
                        run.errors = [{"code": "provider_unavailable", "detail": "No enabled provider is available."}]
                        run.finished_at = datetime.now(UTC)
                        await session.commit()
                        return stats

                    dates = target_dates if target_dates else self._group_dates(group)
                    planned_segments: list[tuple[object, list[date]]] = []
                    self._reset_progress()
                    route_success = 0
                    route_failed = 0

                    for segment in iter_group_segments(group):
                        remaining = await self._filter_already_scraped(
                            session=session,
                            route_group_id=group.id,
                            origin=segment.origin,
                            destinations=segment.destinations,
                            dates=dates,
                        )

                        if not remaining:
                            continue

                        planned_segments.append((segment, remaining))
                        self._planned_checks_total += len(remaining) * len(segment.destinations)

                    self._sync_progress()
                    self._progress["routes_total"] = len(planned_segments)
                    run.routes_total = len(planned_segments)
                    await session.commit()

                    collector = PriceCollector(
                        session_factory=self.session_factory,
                        providers=providers,
                        on_provider_success=self.provider_registry.report_success,
                        on_provider_failure=self.provider_registry.report_failure,
                        on_item_started=lambda origin, destination, depart_date, is_retry: self._record_item_started(
                            origin,
                            destination,
                            depart_date,
                            is_retry,
                        ),
                        on_item_progress=lambda status, origin, destination, depart_date, is_retry: self._record_item_progress(
                            status,
                            origin,
                            destination,
                            depart_date,
                            is_retry,
                        ),
                    )

                    route_semaphore = asyncio.Semaphore(self._route_parallelism(len(planned_segments)))

                    async def collect_planned_segment(segment: object, remaining: list[date]):
                        async with route_semaphore:
                            if self._stop_requested:
                                return {
                                    "success": 0,
                                    "errors": 0,
                                    "skipped": len(remaining) * len(segment.destinations),
                                    "final_missing": 0,
                                }
                            try:
                                return await self._collect_segment_with_retry(
                                    collector=collector,
                                    group=group,
                                    segment=segment,
                                    remaining=remaining,
                                )
                            except Exception as exc:
                                log.exception(
                                    "route_failed",
                                    origin=segment.origin,
                                    error=redact_text(str(exc)),
                                )
                                return {
                                    "success": 0,
                                    "errors": 1,
                                    "skipped": 0,
                                    "final_missing": len(remaining) * len(segment.destinations),
                                }
                            finally:
                                self._progress["routes_done"] += 1
                                self._sync_progress()

                    segment_results = await asyncio.gather(
                        *(
                            collect_planned_segment(segment, remaining)
                            for segment, remaining in planned_segments
                        )
                    )

                    total_final_missing = 0
                    for part in segment_results:
                        stats["success"] += part["success"]
                        stats["errors"] += part["errors"]
                        stats["skipped"] += part["skipped"]
                        total_final_missing += part["final_missing"]
                        if part["success"] > 0 and part["final_missing"] == 0:
                            route_success += 1
                        if part["errors"] > 0 or part["final_missing"] > 0:
                            route_failed += 1

                    if self._stop_requested:
                        run.status = "stopped"
                        run.errors = []
                    elif stats["success"] == 0 and stats["errors"] > 0:
                        run.status = "failed"
                    elif total_final_missing > 0:
                        run.status = "partial"
                        run.errors = [
                            {
                                    "code": "missing_fares",
                                    "detail": (
                                        f"{total_final_missing} date/destination check(s) returned "
                                        "no valid fare after filtering and still need collection."
                                    ),
                            }
                        ]
                    else:
                        run.status = "completed"
                        run.errors = []
                    run.routes_success = route_success
                    run.routes_failed = route_failed
                    run.dates_scraped = stats["success"]
                    run.finished_at = datetime.now(UTC)
                    await session.commit()

                finally:
                    if lock_acquired:
                        try:
                            await self._release_global_lock(session)
                        except Exception:
                            pass

        finally:
            self._is_collecting = False
            self._stop_requested = False

        return stats

    # --------------------------------------------------
    # CLEANUP
    # --------------------------------------------------

    async def cleanup_old_data(self) -> None:
        try:
            async with self.session_factory() as session:
                await session.execute(
                    text(
                        "DELETE FROM scrape_logs "
                        "WHERE created_at < now() - interval '30 days'"
                    )
                )

                await session.execute(
                    text(
                        "DELETE FROM collection_runs "
                        "WHERE started_at < now() - interval '30 days'"
                    )
                )

                await session.execute(
                    text(
                        "DELETE FROM all_flight_results "
                        "WHERE depart_date < current_date - 7"
                    )
                )

                await session.commit()

        except Exception as exc:
            log.exception(
                "cleanup_failed",
                error=redact_text(str(exc)),
            )
