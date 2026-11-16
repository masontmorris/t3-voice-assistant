"""Open-Meteo's geocoding and forecast APIs. They need no key.

A geocoding request sends one place name. A forecast request sends the coordinates geocoding returned. Nothing else
leaves the machine.
"""

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "t3-voice-assistant-dev"


@dataclass(frozen=True)
class Place:
    name: str
    region: str | None
    country: str | None
    latitude: float
    longitude: float
    timezone: str

    def spoken(self):
        area = self.region if self.region and self.region != self.name else self.country
        return ", ".join(part for part in (self.name, area) if part)


@dataclass(frozen=True)
class Day:
    date: date
    high_f: float | None
    precipitation_pct: int | None


def geocode_url(name):
    return GEOCODE + "?" + urllib.parse.urlencode({"name": name, "count": 1})


def forecast_url(place):
    return FORECAST + "?" + urllib.parse.urlencode({
        "latitude": place.latitude, "longitude": place.longitude,
        "daily": "temperature_2m_max,precipitation_probability_max", "timezone": "auto",
        "temperature_unit": "fahrenheit"})


def fetch_json(url, timeout_s):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.load(response)


def parse_place(body):
    """The first geocoding result, or None when the name matched nothing."""
    results = body.get("results") or []
    if not results:
        return None
    first = results[0]
    return Place(first["name"], first.get("admin1"), first.get("country"), first["latitude"], first["longitude"],
                 first["timezone"])


def parse_days(body):
    """The daily forecast, one Day per local date, starting with the place's today."""
    daily = body["daily"]
    return [Day(date.fromisoformat(d), high, pct)
            for d, high, pct in zip(daily["time"], daily["temperature_2m_max"], daily["precipitation_probability_max"])]
