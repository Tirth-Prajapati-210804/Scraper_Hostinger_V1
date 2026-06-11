"""Two-witness enforcement (scrape_enforce_poll_agreement).

A save is only trusted when the DOM-extracted cheapest eligible fare agrees
with Kayak's own poll JSON. Disagreement -> one retry -> refuse (the caller
logs the date extract_failed and it retries under the normal caps).
"""
from __future__ import annotations

import json

import pytest

from app.providers.base import ProviderResult
from app.providers.scrapingbee import ScrapingBeeProvider


def make_provider(enforce: bool) -> ScrapingBeeProvider:
    return ScrapingBeeProvider(
        api_key="test-key",
        timeout=90,
        enforce_poll_agreement=enforce,
    )


def fake_rendered(poll_price: float) -> dict:
    """Rendered payload whose poll JSON has ONE same-airline 1-stop itinerary."""
    poll_body = json.dumps(
        {
            "filteredCount": 1,
            "results": [
                {
                    "legs": [
                        {"segments": [{"id": "1783072800000UA1230101"}, {"id": "1783072800000UA1230102"}]},
                        {"segments": [{"id": "1783072800000UA1230103"}, {"id": "1783072800000UA1230104"}]},
                    ],
                    "bookingOptions": [
                        {"displayPrice": {"price": poll_price, "currency": "CAD"}}
                    ],
                }
            ],
        }
    )
    return {
        "evaluate_results": [],
        "xhr": [{"url": "https://www.ca.kayak.com/i/api/search/dynamic/flights/poll", "body": poll_body}],
        "js_scenario_report": {"total_duration": 40.0, "task_failure": 0},
    }


def eligible_result(price: float) -> ProviderResult:
    return ProviderResult(
        price=price,
        currency="CAD",
        airline="United Airlines",
        deep_link="https://example",
        provider="scrapingbee",
        duration_minutes=600,
        stops=2,
        raw_data={"airline_names": ["United Airlines"], "leg_stops": [1, 1]},
    )


def common_kwargs(rendered: dict, eligible: list[ProviderResult]) -> dict:
    return dict(
        target_url="https://www.ca.kayak.com/flights/X-Y/2026-01-01/Y-X/2026-01-10?x",
        country_code="ca",
        currency="CAD",
        trip_type="round_trip",
        minimum_leg_count=2,
        max_stops=2,
        same_airline_only=True,
        rendered=rendered,
        summary_prices={},
        results=list(eligible),
        card_count=len(eligible),
        captured_count=len(eligible),
        eligible_results=list(eligible),
    )


@pytest.mark.asyncio
async def test_agreement_passes_through_without_retry() -> None:
    provider = make_provider(enforce=True)
    calls: list[dict] = []

    async def fail_if_called(**kwargs):
        calls.append(kwargs)
        raise AssertionError("no retry expected when witnesses agree")

    provider._render_results_attempt = fail_if_called

    *_, eligible, refused, retried = await provider._enforce_two_witness_agreement(
        **common_kwargs(fake_rendered(poll_price=1000.0), [eligible_result(1000.0)]),
        retry_allowed=True,
    )
    assert refused is False and retried is False
    assert [r.price for r in eligible] == [1000.0]
    assert not calls


@pytest.mark.asyncio
async def test_disagreement_retry_recovers_agreement() -> None:
    provider = make_provider(enforce=True)

    async def retry_returns_agreeing_state(**kwargs):
        return (fake_rendered(poll_price=900.0), {}, [eligible_result(900.0)], 1, 1)

    provider._render_results_attempt = retry_returns_agreeing_state

    *_, eligible, refused, retried = await provider._enforce_two_witness_agreement(
        **common_kwargs(fake_rendered(poll_price=900.0), [eligible_result(1000.0)]),
        retry_allowed=True,
    )
    assert retried is True and refused is False
    # The agreeing retry's state is adopted -- the corrected (cheaper) fare wins.
    assert [r.price for r in eligible] == [900.0]


@pytest.mark.asyncio
async def test_persistent_disagreement_is_refused() -> None:
    provider = make_provider(enforce=True)

    async def retry_still_disagrees(**kwargs):
        return (fake_rendered(poll_price=900.0), {}, [eligible_result(1000.0)], 1, 1)

    provider._render_results_attempt = retry_still_disagrees

    *_, refused, retried = await provider._enforce_two_witness_agreement(
        **common_kwargs(fake_rendered(poll_price=900.0), [eligible_result(1000.0)]),
        retry_allowed=True,
    )
    assert retried is True and refused is True


@pytest.mark.asyncio
async def test_disagreement_without_retry_budget_refuses_immediately() -> None:
    provider = make_provider(enforce=True)
    calls: list[dict] = []

    async def fail_if_called(**kwargs):
        calls.append(kwargs)
        raise AssertionError("budget exhausted -> no retry")

    provider._render_results_attempt = fail_if_called

    *_, refused, retried = await provider._enforce_two_witness_agreement(
        **common_kwargs(fake_rendered(poll_price=900.0), [eligible_result(1000.0)]),
        retry_allowed=False,
    )
    assert refused is True and retried is False
    assert not calls


@pytest.mark.asyncio
async def test_enforcement_off_is_a_noop_even_on_disagreement() -> None:
    provider = make_provider(enforce=False)

    *_, eligible, refused, retried = await provider._enforce_two_witness_agreement(
        **common_kwargs(fake_rendered(poll_price=900.0), [eligible_result(1000.0)]),
        retry_allowed=True,
    )
    assert refused is False and retried is False
    assert [r.price for r in eligible] == [1000.0]


@pytest.mark.asyncio
async def test_missing_poll_evidence_is_not_disagreement() -> None:
    provider = make_provider(enforce=True)
    rendered = {"evaluate_results": [], "xhr": []}  # no poll captured

    *_, refused, retried = await provider._enforce_two_witness_agreement(
        **common_kwargs(rendered, [eligible_result(1000.0)]),
        retry_allowed=True,
    )
    assert refused is False and retried is False
