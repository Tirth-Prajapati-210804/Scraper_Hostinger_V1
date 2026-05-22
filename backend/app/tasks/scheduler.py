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
from app.models.scrape_log import ScrapeLog
from app.providers.registry import ProviderRegistry
from app.services.alert_service import AlertService
from app.services.price_collector import PriceCollector
from app.utils.route_segments import iter_group_segments

log = get_logger(__name__)
_DURATION_RETRY_STEPS = (480, 720, 960, 1440, 2160)
_REPEATED_OPERATIONAL_FAILURE_THRESHOLD = 3
_DURATION_RETRY_PAUSE_REASON = "duration_retry_exhausted"
_REPEATED_OPERATIONAL_FAILURE_PAUSE_REASON = "repeated_operational_failures"
_OPERATIONAL_RETRY_PAUSE_REASON = "operational_retry_exhausted"
_GROUP_SAFEGUARD_SUMMARY_CODE = "group_safeguard_summary"
_DEFERRED_DURATION_RETRY_MODE = "duration_fallback"
_DEFERRED_OPERATIONAL_RETRY_MODE = "operational_retry"
_OPERATIONAL_FAILURE_STATUSES = {
    "quota_exhausted",
    "auth_error",
    "rate_limited",
    "parse_error",
    "provider_error",
}
_OPERATIONAL_FAILURE_RESULT_REASONS = {
    "page_empty",
    "extract_failed",
    "parse_error",
    "provider_error",
}


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

    def _next_duration_retry_limit(self, current: int | None) -> int | None:
        if not isinstance(current, int) or current <= 0:
            return None
        for step in _DURATION_RETRY_STEPS:
            if current < step:
                return step
        return None

    def _segment_signature(self, segment: object) -> tuple[str, tuple[str, ...], str, str | None]:
        return (
            str(segment.origin).strip().upper(),
            tuple(str(destination).strip().upper() for destination in (segment.destinations or [])),
            str(segment.trip_type or "one_way"),
            str(segment.return_origin).strip().upper() if getattr(segment, "return_origin", None) else None,
        )

    def _deferred_retry_entry(
        self,
        *,
        segment: object,
        depart_date: date,
        mode: str,
    ) -> dict[str, object]:
        origin, destinations, trip_type, return_origin = self._segment_signature(segment)
        payload: dict[str, object] = {
            "origin": origin,
            "destinations": list(destinations),
            "trip_type": trip_type,
            "depart_date": depart_date.isoformat(),
            "mode": mode,
        }
        if return_origin:
            payload["return_origin"] = return_origin
        return payload

    def _normalize_deferred_retry_entry(self, entry: object) -> dict[str, object] | None:
        if not isinstance(entry, dict):
            return None

        mode = str(entry.get("mode") or "").strip()
        if mode not in {_DEFERRED_DURATION_RETRY_MODE, _DEFERRED_OPERATIONAL_RETRY_MODE}:
            return None

        origin = str(entry.get("origin") or "").strip().upper()
        destinations = [
            str(destination).strip().upper()
            for destination in (entry.get("destinations") or [])
            if str(destination).strip()
        ]
        trip_type = str(entry.get("trip_type") or "one_way").strip() or "one_way"
        return_origin = str(entry.get("return_origin") or "").strip().upper() or None
        raw_depart_date = str(entry.get("depart_date") or "").strip()
        try:
            depart_date = date.fromisoformat(raw_depart_date)
        except ValueError:
            return None

        normalized: dict[str, object] = {
            "origin": origin,
            "destinations": destinations,
            "trip_type": trip_type,
            "depart_date": depart_date.isoformat(),
            "mode": mode,
        }
        if return_origin:
            normalized["return_origin"] = return_origin
        return normalized

    def _deferred_retry_entry_key(self, entry: dict[str, object]) -> tuple[str, str, tuple[str, ...], str, str | None, str]:
        return (
            str(entry.get("mode") or ""),
            str(entry.get("origin") or ""),
            tuple(str(destination) for destination in (entry.get("destinations") or [])),
            str(entry.get("trip_type") or "one_way"),
            str(entry.get("return_origin") or "") or None,
            str(entry.get("depart_date") or ""),
        )

    def _get_group_deferred_retry_state(self, group: RouteGroup) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        seen: set[tuple[str, str, tuple[str, ...], str, str | None, str]] = set()
        for raw_entry in getattr(group, "deferred_retry_state", []) or []:
            normalized = self._normalize_deferred_retry_entry(raw_entry)
            if normalized is None:
                continue
            key = self._deferred_retry_entry_key(normalized)
            if key in seen:
                continue
            seen.add(key)
            entries.append(normalized)
        return entries

    def _set_group_deferred_retry_state(
        self,
        group: RouteGroup,
        entries: list[dict[str, object]],
    ) -> None:
        normalized_entries: list[dict[str, object]] = []
        seen: set[tuple[str, str, tuple[str, ...], str, str | None, str]] = set()
        for raw_entry in entries:
            normalized = self._normalize_deferred_retry_entry(raw_entry)
            if normalized is None:
                continue
            key = self._deferred_retry_entry_key(normalized)
            if key in seen:
                continue
            seen.add(key)
            normalized_entries.append(normalized)
        group.deferred_retry_state = normalized_entries

    def _partition_segment_retry_dates(
        self,
        *,
        group: RouteGroup,
        segment: object,
        dates: list[date],
    ) -> tuple[list[date], list[date], list[date], set[tuple[str, str, tuple[str, ...], str, str | None, str]]]:
        segment_signature = self._segment_signature(segment)
        dates_by_iso = {depart_date.isoformat(): depart_date for depart_date in dates}
        deferred_duration_by_iso: set[str] = set()
        deferred_operational_by_iso: set[str] = set()
        stale_keys: set[tuple[str, str, tuple[str, ...], str, str | None, str]] = set()

        for entry in self._get_group_deferred_retry_state(group):
            entry_signature = (
                str(entry.get("origin") or ""),
                tuple(str(destination) for destination in (entry.get("destinations") or [])),
                str(entry.get("trip_type") or "one_way"),
                str(entry.get("return_origin") or "") or None,
            )
            if entry_signature != segment_signature:
                continue
            key = self._deferred_retry_entry_key(entry)
            depart_date_iso = str(entry.get("depart_date") or "")
            if depart_date_iso not in dates_by_iso:
                stale_keys.add(key)
                continue
            if entry.get("mode") == _DEFERRED_DURATION_RETRY_MODE:
                deferred_duration_by_iso.add(depart_date_iso)
            else:
                deferred_operational_by_iso.add(depart_date_iso)

        fresh_dates = [
            depart_date
            for depart_date in dates
            if depart_date.isoformat() not in deferred_duration_by_iso
            and depart_date.isoformat() not in deferred_operational_by_iso
        ]
        deferred_duration_dates = [
            depart_date for depart_date in dates if depart_date.isoformat() in deferred_duration_by_iso
        ]
        deferred_operational_dates = [
            depart_date for depart_date in dates if depart_date.isoformat() in deferred_operational_by_iso
        ]
        return fresh_dates, deferred_duration_dates, deferred_operational_dates, stale_keys

    async def _latest_scrape_logs_by_key(
        self,
        session: AsyncSession,
        *,
        route_group_id: UUID,
        origin: str,
        destinations: list[str],
        dates: list[date],
    ) -> dict[tuple[str, str, date], dict[str, object]]:
        if not dates or not destinations:
            return {}

        result = await session.execute(
            select(
                ScrapeLog.origin,
                ScrapeLog.destination,
                ScrapeLog.depart_date,
                ScrapeLog.status,
                ScrapeLog.result_reason,
                ScrapeLog.filtered_by_duration,
                ScrapeLog.created_at,
            )
            .where(
                ScrapeLog.route_group_id == route_group_id,
                ScrapeLog.origin == origin,
                ScrapeLog.destination.in_(list(destinations)),
                ScrapeLog.depart_date.in_(list(dates)),
            )
            .order_by(ScrapeLog.created_at.desc())
        )

        latest_by_key: dict[tuple[str, str, date], dict[str, object]] = {}
        for row in result:
            key = (row.origin, row.destination, row.depart_date)
            if key in latest_by_key:
                continue
            latest_by_key[key] = {
                "status": row.status,
                "result_reason": row.result_reason,
                "filtered_by_duration": row.filtered_by_duration,
            }
        return latest_by_key

    async def _classify_missing_retry_dates(
        self,
        *,
        session: AsyncSession,
        route_group_id: UUID,
        segment: object,
        missing_dates: list[date],
    ) -> tuple[list[date], list[date]]:
        latest_logs = await self._latest_scrape_logs_by_key(
            session,
            route_group_id=route_group_id,
            origin=segment.origin,
            destinations=list(segment.destinations),
            dates=missing_dates,
        )
        duration_dates: list[date] = []
        operational_dates: list[date] = []

        for depart_date in missing_dates:
            unresolved_rows: list[dict[str, object]] = []
            for destination in segment.destinations:
                row = latest_logs.get((segment.origin, destination, depart_date))
                if row is None:
                    continue
                if row.get("status") == "success" or row.get("result_reason") == "success":
                    continue
                unresolved_rows.append(row)

            if unresolved_rows and all(
                row.get("result_reason") == "filtered_out"
                and int(row.get("filtered_by_duration") or 0) > 0
                for row in unresolved_rows
            ):
                duration_dates.append(depart_date)
            else:
                operational_dates.append(depart_date)

        return duration_dates, operational_dates

    def _apply_deferred_retry_state_updates(
        self,
        *,
        group: RouteGroup,
        remove_keys: set[tuple[str, str, tuple[str, ...], str, str | None, str]],
        add_entries: list[dict[str, object]],
    ) -> None:
        retained_entries = [
            entry
            for entry in self._get_group_deferred_retry_state(group)
            if self._deferred_retry_entry_key(entry) not in remove_keys
        ]
        retained_entries.extend(add_entries)
        self._set_group_deferred_retry_state(group, retained_entries)

    def _clear_group_safeguard_state(self, group: RouteGroup) -> None:
        group.consecutive_operational_failures = 0
        group.last_operational_failure_at = None
        group.last_auto_pause_reason = None
        group.last_auto_pause_note = None

    def _pause_group(
        self,
        *,
        group: RouteGroup,
        reason: str,
        note: str,
    ) -> None:
        group.is_active = False
        group.last_auto_pause_reason = reason
        group.last_auto_pause_note = note
        group.deferred_retry_state = []

    def _duration_retry_pause_note(self, retry_limit: int) -> str:
        return (
            f"Auto-paused after one duration retry to {retry_limit // 60}h still returned "
            "no valid fare."
        )

    def _operational_retry_pause_note(
        self,
        *,
        exhausted_dates: list[date],
    ) -> str:
        preview = ", ".join(sorted(depart_date.isoformat() for depart_date in exhausted_dates[:3]))
        if len(exhausted_dates) > 3:
            preview = f"{preview} (+{len(exhausted_dates) - 3} more)"
        return (
            "Auto-paused after the follow-up retry run still returned operational failures for "
            f"{len(exhausted_dates)} date(s): {preview}."
        )

    def _repeated_failure_pause_note(
        self,
        *,
        streak: int,
        stats: dict[str, int],
        route_summary: dict[str, int],
        operational_logs: int,
    ) -> str:
        return (
            f"Auto-paused after {streak} consecutive scheduled operational-failure runs "
            f"with no saved fares. Latest run: {route_summary['routes_failed']} route(s) "
            f"still missing, {stats['errors']} collector error(s), {operational_logs} "
            "operational scrape log(s)."
        )

    def _is_operational_failure_log(
        self,
        *,
        status: str | None,
        result_reason: str | None,
    ) -> bool:
        if status in _OPERATIONAL_FAILURE_STATUSES:
            return True
        return status == "no_results" and result_reason in _OPERATIONAL_FAILURE_RESULT_REASONS

    async def _classify_group_run_outcome(
        self,
        *,
        session: AsyncSession,
        group: RouteGroup,
        started_at: datetime,
        stats: dict[str, int],
    ) -> tuple[str, int]:
        if int(stats.get("success", 0) or 0) > 0:
            return "success", 0

        result = await session.execute(
            select(ScrapeLog.status, ScrapeLog.result_reason).where(
                ScrapeLog.route_group_id == group.id,
                ScrapeLog.created_at >= started_at,
            )
        )
        rows = result.all()
        if not rows:
            return "neutral_no_result", 0

        operational_logs = sum(
            1
            for status, result_reason in rows
            if self._is_operational_failure_log(
                status=status,
                result_reason=result_reason,
            )
        )
        if operational_logs == len(rows):
            return "operational_failure", operational_logs
        return "neutral_no_result", operational_logs

    async def _apply_group_failure_safeguard(
        self,
        *,
        session: AsyncSession,
        group: RouteGroup,
        started_at: datetime,
        stats: dict[str, int],
        route_summary: dict[str, int],
        counts_toward_failure_streak: bool,
        retry_summary: dict[str, object] | None = None,
    ) -> dict[str, object]:
        outcome, operational_logs = await self._classify_group_run_outcome(
            session=session,
            group=group,
            started_at=started_at,
            stats=stats,
        )

        auto_pause_triggered = False
        auto_pause_reason = None
        auto_pause_note = None

        if outcome == "success":
            self._clear_group_safeguard_state(group)
        elif counts_toward_failure_streak and outcome == "operational_failure":
            group.consecutive_operational_failures = (
                int(getattr(group, "consecutive_operational_failures", 0) or 0) + 1
            )
            group.last_operational_failure_at = datetime.now(UTC)
            if group.consecutive_operational_failures >= _REPEATED_OPERATIONAL_FAILURE_THRESHOLD:
                auto_pause_reason = _REPEATED_OPERATIONAL_FAILURE_PAUSE_REASON
                auto_pause_note = self._repeated_failure_pause_note(
                    streak=group.consecutive_operational_failures,
                    stats=stats,
                    route_summary=route_summary,
                    operational_logs=operational_logs,
                )
                self._pause_group(
                    group=group,
                    reason=auto_pause_reason,
                    note=auto_pause_note,
                )
                auto_pause_triggered = True

        if retry_summary and retry_summary.get("paused"):
            auto_pause_triggered = True
            auto_pause_reason = str(
                retry_summary.get("pause_reason") or _DURATION_RETRY_PAUSE_REASON
            )
            auto_pause_note = str(retry_summary.get("pause_note") or "")

        summary = {
            "code": _GROUP_SAFEGUARD_SUMMARY_CODE,
            "group_id": str(group.id),
            "group_name": group.name,
            "group_run_outcome": outcome,
            "consecutive_operational_failures": int(
                getattr(group, "consecutive_operational_failures", 0) or 0
            ),
            "auto_pause_triggered": auto_pause_triggered,
            "auto_pause_reason": auto_pause_reason,
            "auto_pause_note": auto_pause_note,
            "counts_toward_failure_streak": counts_toward_failure_streak,
            "saved_fares": int(stats.get("success", 0) or 0),
            "collector_errors": int(stats.get("errors", 0) or 0),
            "checks_skipped": int(stats.get("skipped", 0) or 0),
            "routes_failed": int(route_summary.get("routes_failed", 0) or 0),
            "final_missing": int(route_summary.get("final_missing", 0) or 0),
            "operational_logs": operational_logs,
        }
        if retry_summary:
            summary.update(
                {
                    "deferred_duration_dates": int(retry_summary.get("deferred_duration_dates", 0) or 0),
                    "deferred_operational_dates": int(
                        retry_summary.get("deferred_operational_dates", 0) or 0
                    ),
                    "processed_duration_retries": int(
                        retry_summary.get("processed_duration_retries", 0) or 0
                    ),
                    "processed_operational_retries": int(
                        retry_summary.get("processed_operational_retries", 0) or 0
                    ),
                    "exhausted_duration_dates": int(
                        retry_summary.get("exhausted_duration_dates", 0) or 0
                    ),
                    "exhausted_operational_dates": int(
                        retry_summary.get("exhausted_operational_dates", 0) or 0
                    ),
                }
            )
        return summary

    async def _duration_filtered_retry_dates(
        self,
        session: AsyncSession,
        *,
        route_group_id: UUID,
        origin: str,
        destinations: list[str],
        dates: list[date],
    ) -> list[date]:
        if not dates or not destinations:
            return []

        result = await session.execute(
            select(
                ScrapeLog.origin,
                ScrapeLog.destination,
                ScrapeLog.depart_date,
                ScrapeLog.result_reason,
                ScrapeLog.filtered_by_duration,
                ScrapeLog.created_at,
            )
            .where(
                ScrapeLog.route_group_id == route_group_id,
                ScrapeLog.origin == origin,
                ScrapeLog.destination.in_(list(destinations)),
                ScrapeLog.depart_date.in_(list(dates)),
            )
            .order_by(ScrapeLog.created_at.desc())
        )

        latest_by_key: set[tuple[str, str, date]] = set()
        duration_dates: set[date] = set()
        for row in result:
            key = (row.origin, row.destination, row.depart_date)
            if key in latest_by_key:
                continue
            latest_by_key.add(key)
            if row.result_reason == "filtered_out" and int(row.filtered_by_duration or 0) > 0:
                duration_dates.add(row.depart_date)

        return [depart_date for depart_date in dates if depart_date in duration_dates]

    async def _apply_group_duration_retry(
        self,
        *,
        collector: PriceCollector,
        group: RouteGroup,
        planned_segments: list[tuple[object, list[date]]],
    ) -> dict[str, object]:
        retry_limit = self._next_duration_retry_limit(getattr(group, "max_leg_duration_minutes", None))
        result: dict[str, object] = {
            "success": 0,
            "errors": 0,
            "skipped": 0,
            "paused": False,
            "triggered": False,
            "retry_limit": retry_limit,
            "pause_reason": None,
            "pause_note": None,
            "processed_duration_retries": sum(len(dates) for _, dates in planned_segments),
            "exhausted_duration_dates": 0,
        }
        if retry_limit is None or self._stop_requested:
            return result

        retry_targets: list[tuple[object, list[date]]] = []
        async with self.session_factory() as check_session:
            for segment, dates in planned_segments:
                missing = await self._filter_already_scraped(
                    session=check_session,
                    route_group_id=group.id,
                    origin=segment.origin,
                    destinations=segment.destinations,
                    dates=dates,
                )
                if not missing:
                    continue
                retry_dates = await self._duration_filtered_retry_dates(
                    check_session,
                    route_group_id=group.id,
                    origin=segment.origin,
                    destinations=segment.destinations,
                    dates=missing,
                )
                if retry_dates:
                    retry_targets.append((segment, retry_dates))

        if not retry_targets:
            return result

        result["triggered"] = True
        for segment, retry_dates in retry_targets:
            if self._stop_requested:
                break
            part = await collector.collect_route_batch(
                origin=segment.origin,
                destinations=segment.destinations,
                dates=retry_dates,
                route_group_id=group.id,
                batch_size=self.settings.scrape_batch_size,
                delay_seconds=self.settings.scrape_delay_seconds,
                stop_check=lambda: self._stop_requested,
                market=getattr(group, "market", None),
                currency=group.currency,
                max_stops=group.max_stops,
                same_airline_only=getattr(group, "same_airline_only", False),
                max_leg_duration_minutes=retry_limit,
                trip_type=segment.trip_type,
                nights=segment.nights,
                return_origin=segment.return_origin,
                is_retry=True,
            )
            result["success"] = int(result["success"]) + part["success"]
            result["errors"] = int(result["errors"]) + part["errors"]
            result["skipped"] = int(result["skipped"]) + part["skipped"]

        if self._stop_requested:
            return result

        still_missing = False
        async with self.session_factory() as check_session:
            for segment, retry_dates in retry_targets:
                remaining = await self._filter_already_scraped(
                    session=check_session,
                    route_group_id=group.id,
                    origin=segment.origin,
                    destinations=segment.destinations,
                    dates=retry_dates,
                )
                if remaining:
                    still_missing = True
                    break

        if still_missing:
            pause_note = self._duration_retry_pause_note(retry_limit)
            self._pause_group(
                group=group,
                reason=_DURATION_RETRY_PAUSE_REASON,
                note=pause_note,
            )
            result["paused"] = True
            result["pause_reason"] = _DURATION_RETRY_PAUSE_REASON
            result["pause_note"] = pause_note
            result["exhausted_duration_dates"] = sum(len(dates) for _, dates in retry_targets)
            log.warning(
                "group_paused_after_duration_retry",
                group_id=str(group.id),
                retry_limit=retry_limit,
            )

        return result

    async def _summarize_group_completion(
        self,
        *,
        group: RouteGroup,
        planned_segments: list[tuple[object, list[date]]],
    ) -> dict[str, int]:
        summary = {
            "routes_success": 0,
            "routes_failed": 0,
            "final_missing": 0,
        }
        async with self.session_factory() as check_session:
            for segment, dates in planned_segments:
                remaining = await self._filter_already_scraped(
                    session=check_session,
                    route_group_id=group.id,
                    origin=segment.origin,
                    destinations=segment.destinations,
                    dates=dates,
                )
                final_missing = len(remaining) * len(segment.destinations)
                summary["final_missing"] += final_missing
                if final_missing == 0:
                    summary["routes_success"] += 1
                else:
                    summary["routes_failed"] += 1
        return summary

    async def _collect_dates_for_mode(
        self,
        *,
        collector: PriceCollector,
        group: RouteGroup,
        segment: object,
        dates: list[date],
        mode: str,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "success": 0,
            "errors": 0,
            "skipped": 0,
            "final_missing": 0,
            "new_deferred_entries": [],
            "exhausted_dates": [],
        }
        if not dates or self._stop_requested:
            return result

        max_leg_duration_minutes = getattr(group, "max_leg_duration_minutes", None)
        first_attempt_is_retry = mode != "fresh"
        part = await collector.collect_route_batch(
            origin=segment.origin,
            destinations=segment.destinations,
            dates=dates,
            route_group_id=group.id,
            batch_size=self.settings.scrape_batch_size,
            delay_seconds=self.settings.scrape_delay_seconds,
            stop_check=lambda: self._stop_requested,
            market=getattr(group, "market", None),
            currency=group.currency,
            max_stops=group.max_stops,
            same_airline_only=getattr(group, "same_airline_only", False),
            max_leg_duration_minutes=max_leg_duration_minutes,
            trip_type=segment.trip_type,
            nights=segment.nights,
            return_origin=segment.return_origin,
            is_retry=first_attempt_is_retry,
        )
        result["success"] = int(result["success"]) + part["success"]
        result["errors"] = int(result["errors"]) + part["errors"]
        result["skipped"] = int(result["skipped"]) + part["skipped"]

        if self._stop_requested:
            return result

        async with self.session_factory() as check_session:
            missing = await self._filter_already_scraped(
                session=check_session,
                route_group_id=group.id,
                origin=segment.origin,
                destinations=segment.destinations,
                dates=dates,
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
                max_leg_duration_minutes=max_leg_duration_minutes,
                trip_type=segment.trip_type,
                nights=segment.nights,
                return_origin=segment.return_origin,
                is_retry=True,
            )
            result["success"] = int(result["success"]) + retry["success"]
            result["errors"] = int(result["errors"]) + retry["errors"]
            result["skipped"] = int(result["skipped"]) + retry["skipped"]

            if not self._stop_requested:
                async with self.session_factory() as check_session:
                    missing = await self._filter_already_scraped(
                        session=check_session,
                        route_group_id=group.id,
                        origin=segment.origin,
                        destinations=segment.destinations,
                        dates=missing,
                    )

        if self._stop_requested:
            return result

        result["final_missing"] = len(missing) * len(segment.destinations)
        if not missing:
            return result

        if mode == "fresh":
            async with self.session_factory() as check_session:
                duration_dates, operational_dates = await self._classify_missing_retry_dates(
                    session=check_session,
                    route_group_id=group.id,
                    segment=segment,
                    missing_dates=missing,
                )
            result["new_deferred_entries"] = [
                self._deferred_retry_entry(
                    segment=segment,
                    depart_date=depart_date,
                    mode=_DEFERRED_DURATION_RETRY_MODE,
                )
                for depart_date in duration_dates
            ] + [
                self._deferred_retry_entry(
                    segment=segment,
                    depart_date=depart_date,
                    mode=_DEFERRED_OPERATIONAL_RETRY_MODE,
                )
                for depart_date in operational_dates
            ]
        else:
            result["exhausted_dates"] = missing

        return result

    async def _collect_segment_with_retry(
        self,
        *,
        collector: PriceCollector,
        group: RouteGroup,
        segment: object,
        remaining: list[date],
        deferred_duration_dates: list[date] | None = None,
        deferred_operational_dates: list[date] | None = None,
    ) -> dict[str, object]:
        stats: dict[str, object] = {
            "success": 0,
            "errors": 0,
            "skipped": 0,
            "final_missing": 0,
            "new_deferred_entries": [],
            "remove_deferred_keys": set(),
            "paused": False,
            "pause_reason": None,
            "pause_note": None,
            "deferred_duration_dates": 0,
            "deferred_operational_dates": 0,
            "processed_duration_retries": 0,
            "processed_operational_retries": 0,
            "exhausted_duration_dates": 0,
            "exhausted_operational_dates": 0,
        }

        if self._stop_requested:
            return stats

        self._progress["current_origin"] = segment.origin
        self._progress["current_destination"] = ""
        self._progress["current_date"] = ""

        fresh_dates = [
            depart_date
            for depart_date in remaining
            if depart_date not in set(deferred_duration_dates or [])
            and depart_date not in set(deferred_operational_dates or [])
        ]

        if fresh_dates:
            fresh_result = await self._collect_dates_for_mode(
                collector=collector,
                group=group,
                segment=segment,
                dates=fresh_dates,
                mode="fresh",
            )
            stats["success"] = int(stats["success"]) + int(fresh_result["success"])
            stats["errors"] = int(stats["errors"]) + int(fresh_result["errors"])
            stats["skipped"] = int(stats["skipped"]) + int(fresh_result["skipped"])
            stats["final_missing"] = int(stats["final_missing"]) + int(fresh_result["final_missing"])
            new_entries = list(fresh_result["new_deferred_entries"])
            stats["new_deferred_entries"] = list(stats["new_deferred_entries"]) + new_entries
            stats["deferred_duration_dates"] = int(stats["deferred_duration_dates"]) + sum(
                1
                for entry in new_entries
                if entry.get("mode") == _DEFERRED_DURATION_RETRY_MODE
            )
            stats["deferred_operational_dates"] = int(stats["deferred_operational_dates"]) + sum(
                1
                for entry in new_entries
                if entry.get("mode") == _DEFERRED_OPERATIONAL_RETRY_MODE
            )

        if deferred_operational_dates and not self._stop_requested:
            op_result = await self._collect_dates_for_mode(
                collector=collector,
                group=group,
                segment=segment,
                dates=deferred_operational_dates,
                mode=_DEFERRED_OPERATIONAL_RETRY_MODE,
            )
            stats["success"] = int(stats["success"]) + int(op_result["success"])
            stats["errors"] = int(stats["errors"]) + int(op_result["errors"])
            stats["skipped"] = int(stats["skipped"]) + int(op_result["skipped"])
            stats["final_missing"] = int(stats["final_missing"]) + int(op_result["final_missing"])
            stats["processed_operational_retries"] = int(stats["processed_operational_retries"]) + len(
                deferred_operational_dates
            )
            stats["remove_deferred_keys"] = set(stats["remove_deferred_keys"]) | {
                self._deferred_retry_entry_key(
                    self._deferred_retry_entry(
                        segment=segment,
                        depart_date=depart_date,
                        mode=_DEFERRED_OPERATIONAL_RETRY_MODE,
                    )
                )
                for depart_date in deferred_operational_dates
            }
            exhausted_dates = list(op_result["exhausted_dates"])
            if exhausted_dates:
                stats["paused"] = True
                stats["pause_reason"] = _OPERATIONAL_RETRY_PAUSE_REASON
                stats["pause_note"] = self._operational_retry_pause_note(
                    exhausted_dates=exhausted_dates,
                )
                stats["exhausted_operational_dates"] = int(stats["exhausted_operational_dates"]) + len(
                    exhausted_dates
                )

        if deferred_duration_dates and not self._stop_requested:
            duration_result = await self._apply_group_duration_retry(
                collector=collector,
                group=group,
                planned_segments=[(segment, deferred_duration_dates)],
            )
            stats["success"] = int(stats["success"]) + int(duration_result["success"])
            stats["errors"] = int(stats["errors"]) + int(duration_result["errors"])
            stats["skipped"] = int(stats["skipped"]) + int(duration_result["skipped"])
            stats["processed_duration_retries"] = int(
                stats["processed_duration_retries"]
            ) + int(duration_result["processed_duration_retries"])
            stats["exhausted_duration_dates"] = int(
                stats["exhausted_duration_dates"]
            ) + int(duration_result["exhausted_duration_dates"])
            stats["remove_deferred_keys"] = set(stats["remove_deferred_keys"]) | {
                self._deferred_retry_entry_key(
                    self._deferred_retry_entry(
                        segment=segment,
                        depart_date=depart_date,
                        mode=_DEFERRED_DURATION_RETRY_MODE,
                    )
                )
                for depart_date in deferred_duration_dates
            }
            if duration_result["paused"]:
                stats["paused"] = True
                stats["pause_reason"] = duration_result["pause_reason"]
                stats["pause_note"] = duration_result["pause_note"]

        if not self._stop_requested:
            async with self.session_factory() as check_session:
                final_missing = await self._filter_already_scraped(
                    session=check_session,
                    route_group_id=group.id,
                    origin=segment.origin,
                    destinations=segment.destinations,
                    dates=remaining,
                )
            stats["final_missing"] = len(final_missing) * len(segment.destinations)
        else:
            stats["final_missing"] = 0

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
                    planned_routes: list[tuple[RouteGroup, object, list[date], list[date], list[date]]] = []
                    planned_routes_by_group: dict[UUID, dict[str, object]] = {}
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

                        group_bucket = planned_routes_by_group.setdefault(
                            group.id,
                            {
                                "group": group,
                                "segments": [],
                                "stats": {"success": 0, "errors": 0, "skipped": 0},
                                "retry_summary": {
                                    "paused": False,
                                    "pause_reason": None,
                                    "pause_note": None,
                                    "deferred_duration_dates": 0,
                                    "deferred_operational_dates": 0,
                                    "processed_duration_retries": 0,
                                    "processed_operational_retries": 0,
                                    "exhausted_duration_dates": 0,
                                    "exhausted_operational_dates": 0,
                                },
                                "state_add_entries": [],
                                "state_remove_keys": set(),
                            },
                        )
                        fresh_dates, deferred_duration_dates, deferred_operational_dates, stale_keys = (
                            self._partition_segment_retry_dates(
                                group=group,
                                segment=segment,
                                dates=remaining,
                            )
                        )
                        planned_routes.append(
                            (
                                group,
                                segment,
                                remaining,
                                deferred_duration_dates,
                                deferred_operational_dates,
                            )
                        )
                        group_bucket["segments"].append((segment, remaining))
                        group_bucket["state_remove_keys"] = set(group_bucket["state_remove_keys"]) | stale_keys
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

                    async def collect_planned_route(
                        group: RouteGroup,
                        segment: object,
                        remaining: list[date],
                        deferred_duration_dates: list[date],
                        deferred_operational_dates: list[date],
                    ):
                        async with route_semaphore:
                            if self._stop_requested:
                                return {
                                    "success": 0,
                                    "errors": 0,
                                    "skipped": len(remaining) * len(segment.destinations),
                                    "final_missing": 0,
                                    "new_deferred_entries": [],
                                    "remove_deferred_keys": set(),
                                    "paused": False,
                                    "pause_reason": None,
                                    "pause_note": None,
                                    "deferred_duration_dates": 0,
                                    "deferred_operational_dates": 0,
                                    "processed_duration_retries": 0,
                                    "processed_operational_retries": 0,
                                    "exhausted_duration_dates": 0,
                                    "exhausted_operational_dates": 0,
                                }
                            try:
                                return await self._collect_segment_with_retry(
                                    collector=collector,
                                    group=group,
                                    segment=segment,
                                    remaining=remaining,
                                    deferred_duration_dates=deferred_duration_dates,
                                    deferred_operational_dates=deferred_operational_dates,
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
                                    "new_deferred_entries": [],
                                    "remove_deferred_keys": set(),
                                    "paused": False,
                                    "pause_reason": None,
                                    "pause_note": None,
                                    "deferred_duration_dates": 0,
                                    "deferred_operational_dates": 0,
                                    "processed_duration_retries": 0,
                                    "processed_operational_retries": 0,
                                    "exhausted_duration_dates": 0,
                                    "exhausted_operational_dates": 0,
                                }
                            finally:
                                self._progress["routes_done"] += 1
                                self._sync_progress()

                    route_results = await asyncio.gather(
                        *(
                            collect_planned_route(
                                group,
                                segment,
                                remaining,
                                deferred_duration_dates,
                                deferred_operational_dates,
                            )
                            for group, segment, remaining, deferred_duration_dates, deferred_operational_dates in planned_routes
                        )
                    )

                    pause_errors: list[dict[str, str]] = []
                    for (group, _, _, _, _), stats in zip(planned_routes, route_results):
                        try:
                            total_success += stats["success"]
                            total_errors += stats["errors"]
                            total_skipped += stats["skipped"]
                            group_stats = planned_routes_by_group[group.id]["stats"]
                            group_stats["success"] += int(stats["success"])
                            group_stats["errors"] += int(stats["errors"])
                            group_stats["skipped"] += int(stats["skipped"])
                            group_plan = planned_routes_by_group[group.id]
                            group_plan["state_add_entries"] = list(group_plan["state_add_entries"]) + list(
                                stats["new_deferred_entries"]
                            )
                            group_plan["state_remove_keys"] = set(group_plan["state_remove_keys"]) | set(
                                stats["remove_deferred_keys"]
                            )
                            retry_summary = group_plan["retry_summary"]
                            for field in (
                                "deferred_duration_dates",
                                "deferred_operational_dates",
                                "processed_duration_retries",
                                "processed_operational_retries",
                                "exhausted_duration_dates",
                                "exhausted_operational_dates",
                            ):
                                retry_summary[field] = int(retry_summary[field]) + int(stats[field])
                            if stats["paused"] and not retry_summary["paused"]:
                                retry_summary["paused"] = True
                                retry_summary["pause_reason"] = stats["pause_reason"]
                                retry_summary["pause_note"] = stats["pause_note"]
                                self._pause_group(
                                    group=group_plan["group"],
                                    reason=str(stats["pause_reason"]),
                                    note=str(stats["pause_note"]),
                                )
                                pause_errors.append(
                                    {
                                        "code": str(stats["pause_reason"]),
                                        "detail": str(stats["pause_note"]),
                                    }
                                )

                        except Exception as exc:
                            total_errors += 1
                            self._progress["routes_failed"] += 1

                            log.exception(
                                "route_result_failed",
                                error=redact_text(str(exc)),
                            )

                    for group_plan in planned_routes_by_group.values():
                        self._apply_deferred_retry_state_updates(
                            group=group_plan["group"],
                            remove_keys=set(group_plan["state_remove_keys"]),
                            add_entries=list(group_plan["state_add_entries"]),
                        )

                    total_final_missing = 0
                    route_success = 0
                    route_failed = 0
                    group_safeguards: list[dict[str, object]] = []
                    for group_plan in planned_routes_by_group.values():
                        summary = await self._summarize_group_completion(
                            group=group_plan["group"],
                            planned_segments=group_plan["segments"],
                        )
                        route_success += summary["routes_success"]
                        route_failed += summary["routes_failed"]
                        total_final_missing += summary["final_missing"]
                        group_safeguards.append(
                            await self._apply_group_failure_safeguard(
                                session=session,
                                group=group_plan["group"],
                                started_at=run.started_at,
                                stats=group_plan["stats"],
                                route_summary=summary,
                                counts_toward_failure_streak=True,
                                retry_summary=group_plan["retry_summary"],
                            )
                        )

                    if self._stop_requested:
                        run.status = "stopped"
                        run.errors = []
                    elif total_success == 0 and total_errors > 0:
                        run.status = "failed"
                        run.errors = pause_errors
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
                        ] + pause_errors
                    else:
                        run.status = "completed"
                        run.errors = pause_errors
                    run.safeguards = group_safeguards
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
                    planned_segments: list[tuple[object, list[date], list[date], list[date]]] = []
                    self._reset_progress()
                    route_success = 0
                    route_failed = 0
                    retry_summary: dict[str, object] = {
                        "paused": False,
                        "pause_reason": None,
                        "pause_note": None,
                        "deferred_duration_dates": 0,
                        "deferred_operational_dates": 0,
                        "processed_duration_retries": 0,
                        "processed_operational_retries": 0,
                        "exhausted_duration_dates": 0,
                        "exhausted_operational_dates": 0,
                    }
                    state_add_entries: list[dict[str, object]] = []
                    state_remove_keys: set[tuple[str, str, tuple[str, ...], str, str | None, str]] = set()

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

                        fresh_dates, deferred_duration_dates, deferred_operational_dates, stale_keys = (
                            self._partition_segment_retry_dates(
                                group=group,
                                segment=segment,
                                dates=remaining,
                            )
                        )
                        planned_segments.append(
                            (
                                segment,
                                remaining,
                                deferred_duration_dates,
                                deferred_operational_dates,
                            )
                        )
                        state_remove_keys |= stale_keys
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

                    async def collect_planned_segment(
                        segment: object,
                        remaining: list[date],
                        deferred_duration_dates: list[date],
                        deferred_operational_dates: list[date],
                    ):
                        async with route_semaphore:
                            if self._stop_requested:
                                return {
                                    "success": 0,
                                    "errors": 0,
                                    "skipped": len(remaining) * len(segment.destinations),
                                    "final_missing": 0,
                                    "new_deferred_entries": [],
                                    "remove_deferred_keys": set(),
                                    "paused": False,
                                    "pause_reason": None,
                                    "pause_note": None,
                                    "deferred_duration_dates": 0,
                                    "deferred_operational_dates": 0,
                                    "processed_duration_retries": 0,
                                    "processed_operational_retries": 0,
                                    "exhausted_duration_dates": 0,
                                    "exhausted_operational_dates": 0,
                                }
                            try:
                                return await self._collect_segment_with_retry(
                                    collector=collector,
                                    group=group,
                                    segment=segment,
                                    remaining=remaining,
                                    deferred_duration_dates=deferred_duration_dates,
                                    deferred_operational_dates=deferred_operational_dates,
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
                                    "new_deferred_entries": [],
                                    "remove_deferred_keys": set(),
                                    "paused": False,
                                    "pause_reason": None,
                                    "pause_note": None,
                                    "deferred_duration_dates": 0,
                                    "deferred_operational_dates": 0,
                                    "processed_duration_retries": 0,
                                    "processed_operational_retries": 0,
                                    "exhausted_duration_dates": 0,
                                    "exhausted_operational_dates": 0,
                                }
                            finally:
                                self._progress["routes_done"] += 1
                                self._sync_progress()

                    segment_results = await asyncio.gather(
                        *(
                            collect_planned_segment(
                                segment,
                                remaining,
                                deferred_duration_dates,
                                deferred_operational_dates,
                            )
                            for segment, remaining, deferred_duration_dates, deferred_operational_dates in planned_segments
                        )
                    )

                    for part in segment_results:
                        stats["success"] += part["success"]
                        stats["errors"] += part["errors"]
                        stats["skipped"] += part["skipped"]
                        state_add_entries.extend(list(part["new_deferred_entries"]))
                        state_remove_keys |= set(part["remove_deferred_keys"])
                        for field in (
                            "deferred_duration_dates",
                            "deferred_operational_dates",
                            "processed_duration_retries",
                            "processed_operational_retries",
                            "exhausted_duration_dates",
                            "exhausted_operational_dates",
                        ):
                            retry_summary[field] = int(retry_summary[field]) + int(part[field])
                        if part["paused"] and not retry_summary["paused"]:
                            retry_summary["paused"] = True
                            retry_summary["pause_reason"] = part["pause_reason"]
                            retry_summary["pause_note"] = part["pause_note"]
                            self._pause_group(
                                group=group,
                                reason=str(part["pause_reason"]),
                                note=str(part["pause_note"]),
                            )

                    self._apply_deferred_retry_state_updates(
                        group=group,
                        remove_keys=state_remove_keys,
                        add_entries=state_add_entries,
                    )

                    pause_errors = (
                        [
                            {
                                "code": str(retry_summary["pause_reason"]),
                                "detail": str(retry_summary["pause_note"]),
                            }
                        ]
                        if retry_summary["paused"]
                        else []
                    )

                    summary = await self._summarize_group_completion(
                        group=group,
                        planned_segments=[(segment, remaining) for segment, remaining, _, _ in planned_segments],
                    )
                    total_final_missing = summary["final_missing"]
                    route_success = summary["routes_success"]
                    route_failed = summary["routes_failed"]
                    safeguard_summary = await self._apply_group_failure_safeguard(
                        session=session,
                        group=group,
                        started_at=run.started_at,
                        stats=stats,
                        route_summary=summary,
                        counts_toward_failure_streak=False,
                        retry_summary=retry_summary,
                    )

                    if self._stop_requested:
                        run.status = "stopped"
                        run.errors = []
                    elif stats["success"] == 0 and stats["errors"] > 0:
                        run.status = "failed"
                        run.errors = pause_errors
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
                        ] + pause_errors
                    else:
                        run.status = "completed"
                        run.errors = pause_errors
                    run.safeguards = [safeguard_summary]
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
