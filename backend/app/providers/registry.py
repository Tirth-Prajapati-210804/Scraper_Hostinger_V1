from __future__ import annotations

from time import monotonic

from app.core.config import Settings
from app.core.logging import get_logger
from app.providers.base import (
    FlightProvider,
    ProviderAuthError,
    ProviderBlockedError,
    ProviderQuotaExhaustedError,
    ProviderRateLimitedError,
)
from app.providers.scrapedo import ScrapeDoPoolProvider
from app.providers.scrapingbee import ScrapingBeePoolProvider

log = get_logger(__name__)


class ProviderRegistry:
    """
    Adds:
    - provider health memory
    - temporary cooldown for failing providers
    - Scrape.do primary provider (with ScrapingBee fallback when Scrape.do is unset)
    - cleaner status reporting
    """

    def __init__(self, settings: Settings) -> None:
        self.providers: dict[str, FlightProvider] = {}

        self._cooldowns: dict[str, float] = {}
        self._fail_counts: dict[str, int] = {}
        self._last_status: dict[str, str] = {}

        self._default_cooldown_seconds = 0
        self._rate_limit_cooldown_seconds = 300
        self._blocked_cooldown_seconds = 1800

        concurrency_limit = max(1, settings.provider_concurrency_limit)
        scrapedo_keys = (
            settings.get_scrapedo_keys()
            if callable(getattr(settings, "get_scrapedo_keys", None))
            else [
                key.strip()
                for key in [getattr(settings, "scrapedo_api_key", "")]
                if isinstance(key, str) and key.strip()
            ]
        )
        if scrapedo_keys:
            self.providers["scrapedo"] = ScrapeDoPoolProvider(
                api_keys=scrapedo_keys,
                base_url=getattr(settings, "scrapedo_base_url", "https://api.scrape.do"),
                timeout=settings.provider_timeout_seconds,
                max_retries=settings.provider_max_retries,
                concurrency_limit=concurrency_limit,
                min_delay_seconds=settings.provider_min_delay_seconds,
                country_code=getattr(settings, "scrapedo_country_code", ""),
                super_proxy=getattr(settings, "scrapedo_super_proxy", True),
                initial_poll_delay_seconds=getattr(
                    settings, "scrapedo_initial_poll_delay_seconds", 24.0
                ),
                poll_delay_seconds=getattr(settings, "scrapedo_poll_delay_seconds", 6.0),
                max_polls=getattr(settings, "scrapedo_max_polls", 3),
                user_agent=settings.scrapingbee_user_agent,
            )

        scrapingbee_keys = settings.get_scrapingbee_keys()
        if scrapingbee_keys and "scrapedo" not in self.providers:
            rendered_concurrency_limit = max(
                1,
                min(
                    getattr(settings, "provider_rendered_concurrency_limit", 1),
                    concurrency_limit,
                ),
            )
            self.providers["scrapingbee"] = ScrapingBeePoolProvider(
                api_keys=scrapingbee_keys,
                base_url=settings.scrapingbee_base_url,
                timeout=settings.provider_timeout_seconds,
                max_retries=settings.provider_max_retries,
                concurrency_limit=concurrency_limit,
                rendered_concurrency_limit=rendered_concurrency_limit,
                min_delay_seconds=settings.provider_min_delay_seconds,
                country_code=settings.scrapingbee_country_code,
                premium_proxy=settings.scrapingbee_premium_proxy,
                stealth_proxy=settings.scrapingbee_stealth_proxy,
                multi_city_debug=settings.scrapingbee_multi_city_debug,
                kayak_homepage_extract=getattr(
                    settings, "scrapingbee_kayak_homepage_extract", False
                ),
                kayak_result_wait_ms=getattr(
                    settings, "scrapingbee_kayak_result_wait_ms", 30_000
                ),
                user_agent=settings.scrapingbee_user_agent,
                enforce_poll_agreement=getattr(
                    settings, "scrape_enforce_poll_agreement", False
                ),
            )

    # --------------------------------------------------
    # INTERNAL
    # --------------------------------------------------

    def _is_cooled_down(self, name: str) -> bool:
        until = self._cooldowns.get(name, 0.0)
        return monotonic() < until

    def _set_cooldown(
        self,
        name: str,
        seconds: int,
    ) -> None:
        self._cooldowns[name] = monotonic() + seconds

        log.warning(
            "provider_cooldown_started",
            provider=name,
            cooldown_seconds=seconds,
        )

    def _clear_health(self, name: str) -> None:
        self._fail_counts[name] = 0
        self._cooldowns.pop(name, None)
        self._last_status.pop(name, None)

    # --------------------------------------------------
    # PUBLIC
    # --------------------------------------------------

    def get_enabled(self) -> list[FlightProvider]:
        """
        Returns healthy providers only.
        """

        healthy: list[FlightProvider] = []

        for name, provider in self.providers.items():
            if not provider.is_configured():
                continue

            if self._is_cooled_down(name):
                continue

            healthy.append(provider)

        return healthy

    def report_success(self, provider_name: str) -> None:
        self._clear_health(provider_name)

    def report_failure(
        self,
        provider_name: str,
        exc: BaseException,
    ) -> None:
        """
        Called by services when provider fails.
        """

        self._fail_counts[provider_name] = (
            self._fail_counts.get(provider_name, 0) + 1
        )

        failures = self._fail_counts[provider_name]
        self._last_status[provider_name] = "error"

        if isinstance(exc, ProviderQuotaExhaustedError):
            self._last_status[provider_name] = "quota_exhausted"
            self._set_cooldown(
                provider_name,
                3600,
            )
            return

        if isinstance(exc, ProviderAuthError):
            self._last_status[provider_name] = "auth_error"
            self._set_cooldown(
                provider_name,
                86400,
            )
            return

        if isinstance(exc, ProviderRateLimitedError):
            self._last_status[provider_name] = "rate_limited"
            self._set_cooldown(
                provider_name,
                self._rate_limit_cooldown_seconds,
            )
            return

        if isinstance(exc, ProviderBlockedError):
            self._last_status[provider_name] = "blocked"
            self._set_cooldown(provider_name, self._blocked_cooldown_seconds)
            return

        # generic repeated failures
        if failures >= 5:
            self._set_cooldown(
                provider_name,
                self._default_cooldown_seconds,
            )

    def status(self) -> dict[str, str]:
        """
        UI-friendly provider status.
        """

        result: dict[str, str] = {
            "scrapedo": "disabled",
            "scrapingbee": "disabled",
        }

        for name, provider in self.providers.items():
            if not provider.is_configured():
                result[name] = "disabled"
                continue

            if self._is_cooled_down(name):
                result[name] = self._last_status.get(name, "cooldown")
            else:
                result[name] = "configured"

        return result

    async def close_all(self) -> None:
        for provider in self.providers.values():
            try:
                await provider.close()
            except Exception:
                pass
