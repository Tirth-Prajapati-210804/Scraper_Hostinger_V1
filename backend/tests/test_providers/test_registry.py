from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.providers.base import ProviderAuthError, ProviderBlockedError, ProviderQuotaExhaustedError
from app.providers.registry import ProviderRegistry


def make_settings(**overrides) -> MagicMock:
    settings = MagicMock()
    settings.scrapedo_api_key = ""
    settings.scrapedo_api_keys = ""
    settings.scrapedo_base_url = "https://api.scrape.do"
    settings.scrapedo_country_code = ""
    settings.scrapedo_super_proxy = True
    settings.scrapedo_initial_poll_delay_seconds = 45.0
    settings.scrapedo_poll_delay_seconds = 15.0
    settings.scrapedo_max_polls = 1
    settings.scrapedo_allow_mult_fallback = False
    settings.scrapingbee_api_key = ""
    settings.scrapingbee_api_keys = ""
    settings.scrapingbee_base_url = "https://app.scrapingbee.com/api/v1"
    settings.scrapingbee_country_code = "us"
    settings.scrapingbee_user_agent = "flight-harvester/1.0"
    settings.scrapingbee_premium_proxy = False
    settings.scrapingbee_stealth_proxy = False
    settings.scrapingbee_multi_city_debug = False
    settings.scrapingbee_kayak_homepage_extract = True
    settings.scrapingbee_kayak_result_wait_ms = 35_000
    settings.provider_timeout_seconds = 30
    settings.provider_max_retries = 3
    settings.provider_concurrency_limit = 2
    settings.provider_rendered_concurrency_limit = 1
    settings.provider_min_delay_seconds = 0.5
    for k, v in overrides.items():
        setattr(settings, k, v)
    settings.get_scrapedo_keys = lambda: [
        key
        for key in [settings.scrapedo_api_key, *settings.scrapedo_api_keys.split(",")]
        if isinstance(key, str) and key.strip()
    ]
    settings.get_scrapingbee_keys = lambda: [
        key
        for key in [settings.scrapingbee_api_key, *settings.scrapingbee_api_keys.split(",")]
        if isinstance(key, str) and key.strip()
    ]
    return settings


def test_no_providers_when_no_key() -> None:
    registry = ProviderRegistry(make_settings())
    assert registry.get_enabled() == []


def test_scrapingbee_key_creates_scrapingbee_provider() -> None:
    registry = ProviderRegistry(make_settings(scrapingbee_api_key="bee-key-123"))
    providers = registry.get_enabled()
    assert len(providers) == 1
    assert providers[0].name == "scrapingbee"


def test_scrapingbee_debug_flag_is_passed_to_provider() -> None:
    registry = ProviderRegistry(
        make_settings(
            scrapingbee_api_key="bee-key-123",
            scrapingbee_multi_city_debug=True,
        )
    )

    provider = registry.get_enabled()[0]

    assert provider._providers[0]._multi_city_debug is True


def test_scrapingbee_kayak_homepage_extract_flag_is_passed_to_provider() -> None:
    registry = ProviderRegistry(
        make_settings(
            scrapingbee_api_key="bee-key-123",
            scrapingbee_kayak_homepage_extract=True,
        )
    )

    assert registry.get_enabled()[0]._providers[0]._kayak_homepage_extract is True
    assert registry.get_enabled()[0]._providers[0]._kayak_result_wait_ms == 35_000


def test_scrapingbee_provider_respects_configured_concurrency_limit() -> None:
    registry = ProviderRegistry(
        make_settings(
            scrapingbee_api_key="bee-key-123",
            provider_concurrency_limit=1,
        )
    )

    provider = registry.get_enabled()[0]

    assert provider._providers[0]._semaphore._value == 1


def test_scrapingbee_provider_uses_dedicated_rendered_limit() -> None:
    registry = ProviderRegistry(
        make_settings(
            scrapingbee_api_key="bee-key-123",
            provider_concurrency_limit=3,
            provider_rendered_concurrency_limit=1,
        )
    )

    provider = registry.get_enabled()[0]

    assert provider._providers[0]._semaphore._value == 3
    assert provider._providers[0]._rendered_semaphore._value == 1

def test_status_scrapingbee_configured() -> None:
    registry = ProviderRegistry(make_settings(scrapingbee_api_key="bee-key"))
    status = registry.status()
    assert status["scrapingbee"] == "configured"


def test_quota_failure_sets_scrapingbee_quota_status_and_disables_provider() -> None:
    registry = ProviderRegistry(make_settings(scrapingbee_api_key="bee-key"))
    registry.report_failure("scrapingbee", ProviderQuotaExhaustedError("quota hit"))

    assert registry.get_enabled() == []
    assert registry.status()["scrapingbee"] == "quota_exhausted"


def test_success_clears_scrapingbee_failure_status() -> None:
    registry = ProviderRegistry(make_settings(scrapingbee_api_key="bee-key"))
    registry.report_failure("scrapingbee", ProviderAuthError("bad key"))
    registry.report_success("scrapingbee")

    providers = registry.get_enabled()
    assert len(providers) == 1
    assert registry.status()["scrapingbee"] == "configured"


def test_kayak_security_block_cools_down_provider() -> None:
    registry = ProviderRegistry(make_settings(scrapingbee_api_key="bee-key"))

    registry.report_failure("scrapingbee", ProviderBlockedError("security check"))

    assert registry.get_enabled() == []
    assert registry.status()["scrapingbee"] == "blocked"


def test_status_nothing_configured() -> None:
    registry = ProviderRegistry(make_settings())
    status = registry.status()
    assert status["scrapedo"] == "disabled"
    assert status["scrapingbee"] == "disabled"


def test_scrapedo_key_creates_primary_scrapedo_provider() -> None:
    registry = ProviderRegistry(
        make_settings(
            scrapedo_api_key="sd-key-123",
            scrapingbee_api_key="bee-key-456",
        )
    )
    providers = registry.get_enabled()
    assert len(providers) == 1
    assert providers[0].name == "scrapedo"
    status = registry.status()
    assert status["scrapedo"] == "configured"
    assert status["scrapingbee"] == "disabled"


def test_scrapedo_failure_cools_down_provider() -> None:
    registry = ProviderRegistry(make_settings(scrapedo_api_key="sd-key-123"))
    registry.report_failure("scrapedo", ProviderBlockedError("security check"))
    assert registry.get_enabled() == []
    assert registry.status()["scrapedo"] == "blocked"
    registry.report_success("scrapedo")
    assert len(registry.get_enabled()) == 1


def test_scrapedo_settings_forwarded_to_provider() -> None:
    registry = ProviderRegistry(
        make_settings(
            scrapedo_api_key="sd-key-123",
            scrapedo_initial_poll_delay_seconds=45.0,
            scrapedo_poll_delay_seconds=15.0,
            scrapedo_max_polls=1,
            scrapedo_allow_mult_fallback=False,
        )
    )
    pool = registry.get_enabled()[0]
    inner = pool._providers[0]
    assert inner._initial_poll_delay_seconds == 45.0
    assert inner._poll_delay_seconds == 15.0
    assert inner._max_polls == 1
    assert inner._allow_mult_fallback is False


@pytest.mark.asyncio
async def test_close_all() -> None:
    registry = ProviderRegistry(make_settings(scrapingbee_api_key="bee-key"))
    await registry.close_all()
