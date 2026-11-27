from __future__ import annotations

from app.providers.base import FlightProvider, ProviderResult
from app.providers.registry import ProviderRegistry
from app.providers.scrapedo import ScrapeDoPoolProvider, ScrapeDoProvider
from app.providers.scrapingbee import ScrapingBeePoolProvider, ScrapingBeeProvider

__all__ = [
    "FlightProvider",
    "ProviderResult",
    "ProviderRegistry",
    "ScrapeDoPoolProvider",
    "ScrapeDoProvider",
    "ScrapingBeePoolProvider",
    "ScrapingBeeProvider",
]
