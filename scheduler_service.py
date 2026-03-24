# Scheduler service: query building and job definitions for scheduled runs

from typing import Any


SUBJECTS = ["jobs", "weather", "stocks", "news", "custom"]

DEFAULT_JOB_SITES = [
    "gov.sk.ca/careers",
    "sgi.sk.ca/careers",
    "saskatchewan.ca/careers",
    "Crown corporations Saskatchewan job boards",
]


def build_query(subject: str, params: dict[str, Any]) -> str:
    """Build agent query from job subject and params."""
    subject = (subject or "custom").lower().strip()
    params = params or {}

    if subject == "custom":
        return (params.get("query") or params.get("custom_query") or "").strip() or "Summarize today."

    if subject == "jobs":
        qualification = params.get("qualification", params.get("qualifications", "my qualifications"))
        sites = params.get("sites") or params.get("urls") or DEFAULT_JOB_SITES
        sites_str = ", ".join(sites) if isinstance(sites, list) else str(sites)
        return (
            f"Visit Crown and government job sites in Saskatchewan (e.g. {sites_str}). "
            f"Find jobs relevant to: {qualification}. "
            f"For each job, extract: company name, job title, description, and URL. "
            f"Return a structured table or list with company name, job title, description, and URL."
        )

    if subject == "weather":
        location = params.get("location", params.get("city", "Saskatoon, Saskatchewan"))
        return (
            f"What is the current weather in {location}? "
            f"Include temperature, conditions, humidity, wind, and a brief summary."
        )

    if subject == "stocks":
        ticker = params.get("ticker", params.get("symbol", "SPY"))
        return (
            f"Get the latest stock data and brief analysis for {ticker}. "
            f"Include price, change, volume, and a short outlook."
        )

    if subject == "news":
        topic = params.get("topic", params.get("query", "technology"))
        return (
            f"Get the latest news about {topic}. "
            f"Include headline, summary, source, and URL for each article."
        )

    return (params.get("query") or f"Research: {subject}").strip()
