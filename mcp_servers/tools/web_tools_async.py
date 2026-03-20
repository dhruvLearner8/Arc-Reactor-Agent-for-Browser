# web_tools_async.py
import asyncio
import traceback
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
from readability import Document
import trafilatura
import random
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

# MCP Protocol Safety: Redirect print to stderr
def print(*args, **kwargs):
    sys.stderr.write(" ".join(map(str, args)) + "\n")
    sys.stderr.flush()

DIFFICULT_WEBSITES_PATH = Path(__file__).parent / "difficult_websites.txt"

def get_random_headers():
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/537.36 Chrome/113.0.5672.92 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_2) AppleWebKit/605.1.15 Version/16.3 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
        "Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 Chrome/117.0.5938.132 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; SAMSUNG SM-G998B) AppleWebKit/537.36 Chrome/92.0.4515.159 Mobile Safari/537.36 SamsungBrowser/15.0",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile Safari/604.1",
        "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 Version/16.6 Mobile Safari/604.1"
    ]
    return {"User-Agent": random.choice(user_agents)}


def is_difficult_website(url: str) -> bool:
    if not DIFFICULT_WEBSITES_PATH.exists():
        return False
    try:
        with open(DIFFICULT_WEBSITES_PATH, "r", encoding="utf-8") as f:
            difficult_sites = [line.strip().lower() for line in f if line.strip()]
        return any(domain in url.lower() for domain in difficult_sites)
    except Exception as e:
        print(f"⚠️ Failed to read difficult_websites.txt: {e}")
        return False

# Make sure these utilities exist
def ascii_only(text: str) -> str:
    return text.encode("ascii", errors="ignore").decode()

def choose_best_text(visible, main, trafilatura_):
    candidates = {
        "visible": visible or "",
        "main": main or "",
        "trafilatura": trafilatura_ or ""
    }
    scores = {name: score_text_quality(text) for name, text in candidates.items()}
    best = max(scores, key=scores.get)
    return candidates[best], best, scores


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme:
        return f"https://{url}"
    return url


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def score_text_quality(text: str) -> float:
    text = (text or "").strip()
    if not text:
        return 0.0

    length = len(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    unique_lines = len(set(lines)) if lines else 0
    unique_ratio = unique_lines / max(len(lines), 1)

    words = text.split()
    word_count = len(words)
    avg_word_len = sum(len(w) for w in words) / max(word_count, 1)

    blocked_markers = [
        "enable javascript",
        "access denied",
        "verify you are human",
        "captcha",
        "cloudflare",
        "robot check"
    ]
    marker_penalty = 4000 if any(m in text.lower() for m in blocked_markers) else 0

    score = (
        min(length, 12000) * 1.0
        + min(word_count * 3, 4000)
        + (unique_ratio * 1500)
        + min(avg_word_len * 120, 800)
        - marker_penalty
    )
    return float(score)


def should_fallback_to_browser(best_text: str, quality_score: float, content_type: str = "", status_code: int = 200) -> bool:
    low_quality = len((best_text or "").strip()) < 450 or quality_score < 1200
    likely_blocked = any(
        marker in (best_text or "").lower()
        for marker in ["enable javascript", "captcha", "cloudflare", "access denied"]
    )
    bad_content_type = bool(content_type) and "text/html" not in content_type.lower()
    bad_status = status_code >= 400
    return low_quality or likely_blocked or bad_content_type or bad_status


async def fetch_html_with_retries(url: str, timeout: int = 8, retries: int = 2):
    import httpx

    last_error = None
    for attempt in range(retries + 1):
        headers = get_random_headers()
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url, headers=headers)
                if response.status_code >= 500 and attempt < retries:
                    raise httpx.HTTPStatusError(
                        f"Server error {response.status_code}",
                        request=response.request,
                        response=response
                    )
                html = response.content.decode("utf-8", errors="replace")
                return response, html, attempt + 1, None
        except Exception as e:
            last_error = e
            if attempt < retries:
                await asyncio.sleep(0.35 * (2 ** attempt) + random.uniform(0.05, 0.2))
                continue
    return None, "", retries + 1, last_error

async def web_tool_playwright(url: str, max_total_wait: int = 15) -> dict:
    result = {"url": url}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True) # changed to headless=True for stability
            page = await browser.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=max_total_wait * 1000)

            # Wait until the page body has significant content (i.e., text is non-trivial)
            try:
                await page.wait_for_function(
                    """() => {
                        const body = document.querySelector('body');
                        return body && (body.innerText || "").length > 1000;
                    }""",
                    timeout=max_total_wait * 1000
                )
            except Exception as e:
                print("⚠️ Generic wait failed:", e)

            # Small additional wait for late-rendering content.
            await asyncio.sleep(2)

            try:
                await page.evaluate("""() => {
                    window.stop();
                    document.querySelectorAll('script').forEach(s => s.remove());
                }""")
            except Exception as e:
                print("⚠️ JS stop failed:", e)

            html = await page.content()
            visible_text = await page.inner_text("body")
            title = await page.title()
            await browser.close()

            # Run parsing in background to free browser early
            try:
                main_text = await asyncio.to_thread(lambda: BeautifulSoup(Document(html).summary(), "html.parser").get_text(separator="\n", strip=True))
            except Exception as e:
                print("⚠️ Readability failed:", e)
                main_text = ""

            try:
                trafilatura_text = await asyncio.to_thread(lambda: trafilatura.extract(html) or "")
            except Exception as e:
                print("⚠️ Trafilatura failed:", e)
                trafilatura_text = ""

            best_text, source, quality_scores = choose_best_text(visible_text, main_text, trafilatura_text)

            result.update({
                "title": title,
                "html": html,
                "text": visible_text,
                "main_text": main_text,
                "trafilatura_text": trafilatura_text,
                "best_text": ascii_only(best_text),
                "best_text_source": source,
                "quality_scores": quality_scores,
                "status": "ok_playwright"
            })

    except PlaywrightTimeoutError:
        result.update({
            "title": "[timeout: goto]",
            "html": "",
            "text": "[timed out]",
            "main_text": "[no HTML extracted]",
            "trafilatura_text": "",
            "best_text": "[no text]",
            "best_text_source": "timeout",
            "quality_scores": {},
            "status": "timeout_playwright"
        })

    except Exception as e:
        traceback.print_exc()
        result.update({
            "title": "[error]",
            "html": "",
            "text": f"[error: {e}]",
            "main_text": "[no HTML extracted]",
            "trafilatura_text": "",
            "best_text": "[no text]",
            "best_text_source": "error",
            "quality_scores": {},
            "status": "error_playwright"
        })

    return result

async def smart_web_extract(url: str, timeout: int = 5) -> dict:
    started_at = time.time()
    normalized_url = normalize_url(url)
    if not is_valid_url(normalized_url):
        return {
            "url": url,
            "title": "[invalid-url]",
            "html": "",
            "text": "[invalid url format]",
            "main_text": "",
            "trafilatura_text": "",
            "best_text": "[no text]",
            "best_text_source": "invalid",
            "quality_scores": {},
            "status": "invalid_url"
        }

    try:
        if is_difficult_website(normalized_url):
            print(f"Detected difficult site ({normalized_url}) -> skipping fast scrape")
            result = await web_tool_playwright(normalized_url, max_total_wait=18)
            result["latency_ms"] = int((time.time() - started_at) * 1000)
            return result

        response, html, attempts, fetch_error = await fetch_html_with_retries(
            normalized_url, timeout=max(timeout, 6), retries=2
        )
        if fetch_error or not response:
            print("Fast scrape network fetch failed:", fetch_error)
            result = await web_tool_playwright(normalized_url, max_total_wait=18)
            result["latency_ms"] = int((time.time() - started_at) * 1000)
            result["fallback_reason"] = "network_fetch_failed"
            return result

        doc = Document(html)
        main_html = doc.summary()
        main_text = BeautifulSoup(main_html, "html.parser").get_text(separator="\n", strip=True)
        visible_text = BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)
        trafilatura_text = trafilatura.extract(html) or ""
        best_text, best_source, quality_scores = choose_best_text(visible_text, main_text, trafilatura_text)
        best_quality = quality_scores.get(best_source, 0.0)
        content_type = response.headers.get("content-type", "")

        if not should_fallback_to_browser(best_text, best_quality, content_type, response.status_code):
            return {
                "url": normalized_url,
                "title": Document(html).short_title(),
                "html": html,
                "text": visible_text,
                "main_text": main_text,
                "trafilatura_text": trafilatura_text,
                "best_text": ascii_only(best_text),
                "best_text_source": best_source,
                "quality_scores": quality_scores,
                "status": "ok_fast",
                "http_status": response.status_code,
                "http_content_type": content_type,
                "attempts": attempts,
                "latency_ms": int((time.time() - started_at) * 1000)
            }

        print("Fast scrape quality too low, falling back to Playwright...")

    except Exception as e:
        print("Fast scrape failed:", e)

    # Fallback
    fallback_result = await web_tool_playwright(normalized_url, max_total_wait=18)
    fallback_result["latency_ms"] = int((time.time() - started_at) * 1000)
    fallback_result["fallback_reason"] = "low_quality_or_exception"
    return fallback_result


if __name__ == "__main__":
    print("starting scrape subprocess...")
    import sys
    import json

    if len(sys.argv) != 2:
        print("Usage: python web_tool_playwright_async.py <url>")
        sys.exit(1)

    url = sys.argv[1]
    print("🚀 Trying smart scrape first...")
    result = asyncio.run(smart_web_extract(url))
    print(json.dumps(result, ensure_ascii=False))
