"""
MCP server: live weather via Open-Meteo (free, no API key).
Use this instead of web scraping for "weather in <city>" queries.
"""
from __future__ import annotations

import json
import sys

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("open-meteo-weather")

# WMO Weather interpretation codes (subset)
_WMO = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


@mcp.tool()
async def get_current_weather(city_or_place: str) -> str:
    """
    Current weather for a city or place (e.g. Toronto, Regina, London, Kashmir region).
    Returns JSON: temperature (°C), humidity, wind, conditions, resolved location, time.
    Prefer this tool whenever the user asks for weather, forecast basics, or conditions — no web scrape needed.
    """
    place = (city_or_place or "").strip()
    if not place:
        return json.dumps({"error": "empty_location", "message": "Pass a city or region name."})

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            geo_r = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": place, "count": "3", "language": "en", "format": "json"},
            )
            geo_r.raise_for_status()
            geo = geo_r.json()
            results = geo.get("results") or []
            if not results:
                return json.dumps(
                    {
                        "error": "geocode_not_found",
                        "query": place,
                        "message": "No location match. Try a larger city or add country (e.g. Paris France).",
                    }
                )

            loc = results[0]
            lat, lon = loc["latitude"], loc["longitude"]
            label = loc.get("name", place)
            admin = loc.get("admin1") or ""
            country = loc.get("country_code") or ""
            loc_parts = [label, admin, country]
            resolved = ", ".join(p for p in loc_parts if p)

            fc_r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": ",".join(
                        [
                            "temperature_2m",
                            "relative_humidity_2m",
                            "apparent_temperature",
                            "weather_code",
                            "wind_speed_10m",
                            "wind_direction_10m",
                            "surface_pressure",
                        ]
                    ),
                    "timezone": "auto",
                },
            )
            fc_r.raise_for_status()
            fc = fc_r.json()
            cur = fc.get("current") or {}
            code = cur.get("weather_code")
            code_i = int(code) if code is not None else -1
            conditions = _WMO.get(code_i, f"weather_code_{code_i}")

            out = {
                "resolved_location": resolved,
                "latitude": lat,
                "longitude": lon,
                "local_time": cur.get("time"),
                "temperature_c": cur.get("temperature_2m"),
                "feels_like_c": cur.get("apparent_temperature"),
                "relative_humidity_percent": cur.get("relative_humidity_2m"),
                "wind_speed_kmh": cur.get("wind_speed_10m"),
                "wind_direction_deg": cur.get("wind_direction_10m"),
                "surface_pressure_hpa": cur.get("surface_pressure"),
                "conditions": conditions,
                "weather_code": code_i,
                "data_source": "Open-Meteo (https://open-meteo.com)",
            }
            return json.dumps(out, indent=2)
    except httpx.HTTPError as e:
        return json.dumps({"error": "http_error", "message": str(e)})
    except Exception as e:
        return json.dumps({"error": "weather_tool_failed", "message": str(e)})


if __name__ == "__main__":
    mcp.run(transport="stdio")
