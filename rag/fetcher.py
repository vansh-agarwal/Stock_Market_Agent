"""
Fetcher module: auto-downloads filings from BSE, NSE, and RBI.

Each fetcher returns a list of document dicts with keys:
    text      - extracted text content
    source    - "BSE" | "NSE" | "RBI"
    date      - publication date string
    ticker    - normalised ticker symbol (no .NS/.BO suffix)
    url       - source URL for citation
    doc_id    - unique identifier for the document
"""

import time
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# BSE scrip-code mapping (NSE symbol -> BSE numeric code)
# ---------------------------------------------------------------------------
BSE_SCRIP_CODES: Dict[str, str] = {
    "RELIANCE":    "500325",
    "TCS":         "532540",
    "INFY":        "500209",
    "HDFCBANK":    "500180",
    "ICICIBANK":   "532174",
    "HINDUNILVR":  "500696",
    "SBIN":        "500112",
    "BAJFINANCE":  "500034",
    "BHARTIARTL":  "532454",
    "KOTAKBANK":   "500247",
    "WIPRO":       "507685",
    "LT":          "500510",
    "ASIANPAINT":  "500820",
    "AXISBANK":    "532215",
    "MARUTI":      "532500",
    "TITAN":       "500114",
    "ULTRACEMCO":  "532538",
    "SUNPHARMA":   "524715",
    "HCLTECH":     "532281",
    "NESTLEIND":   "500790",
    "TECHM":       "532755",
    "POWERGRID":   "532898",
    "NTPC":        "532555",
    "ONGC":        "500312",
    "COALINDIA":   "533278",
    "BAJAJFINSV":  "532978",
    "TATAMOTORS":  "500570",
    "TATASTEEL":   "500470",
    "ADANIPORTS":  "532921",
    "ADANIENT":    "512599",
    "INDUSINDBK":  "532187",
    "GRASIM":      "500300",
    "CIPLA":       "500087",
    "DRREDDY":     "500124",
    "EICHERMOT":   "505200",
    "HEROMOTOCO":  "500182",
    "BPCL":        "500547",
    "IOC":         "530965",
    "MM":          "500520",
    "DIVISLAB":    "532488",
    "BRITANNIA":   "500825",
    "PIDILITIND":  "500331",
    "GODREJCP":    "532424",
    "DABUR":       "500096",
    "HAVELLS":     "517354",
    "SIEMENS":     "500550",
    "ABB":         "500002",
    "BOSCHLTD":    "500530",
    "APOLLOHOSP":  "508869",
    "TATACONSUM":  "500800",
    "ZOMATO":      "543320",
    "NYKAA":       "543384",
    "PAYTM":       "543396",
    "DMART":       "540376",
    "IRCTC":       "542830",
    "LICI":        "543526",
}

# Shared browser-like headers
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_BSE_HEADERS = {
    **_BROWSER_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
    "Origin":  "https://www.bseindia.com",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_ticker(ticker: str) -> str:
    """Strip exchange suffix to get clean NSE symbol (e.g. RELIANCE.NS -> RELIANCE)."""
    return ticker.replace(".NS", "").replace(".BO", "").upper()


def _stable_id(prefix: str, *parts: str) -> str:
    """
    Build a stable, collision-resistant document ID from arbitrary string parts.
    Uses MD5 (not Python's randomised hash()) so the same inputs always produce
    the same ID across process restarts — required for ChromaDB upsert idempotency.
    """
    raw = "|".join(parts)
    digest = hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{prefix}_{digest}"


# ---------------------------------------------------------------------------
# BSE
# ---------------------------------------------------------------------------

def get_bse_scrip_code(ticker: str) -> Optional[str]:
    """
    Return the BSE numeric scrip code for a given ticker.
    Tries the hardcoded dict first, then a live BSE API lookup.
    """
    symbol = _normalize_ticker(ticker)

    if symbol in BSE_SCRIP_CODES:
        return BSE_SCRIP_CODES[symbol]

    # Live fallback: search BSE's equity scrip list
    try:
        url = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
        params = {
            "Group": "", "Scripcode": "", "Segment": "Equity",
            "Status": "Active", "industry": "", "Response": "Json",
        }
        resp = requests.get(url, params=params, headers=_BSE_HEADERS, timeout=12)
        if resp.ok:
            for item in resp.json().get("Table", []):
                if item.get("NSE_Symbol", "").upper() == symbol:
                    code = str(item.get("Scrip_Cd", ""))
                    if code:
                        BSE_SCRIP_CODES[symbol] = code  # cache for session
                        return code
    except Exception as e:
        print(f"[fetcher] BSE scrip-code lookup failed: {e}")

    return None


def fetch_bse_announcements(ticker: str, days: int = 90) -> List[Dict[str, Any]]:
    """
    Fetch recent corporate announcements from BSE for *ticker*.

    Args:
        ticker: e.g. "RELIANCE.NS"
        days:   how many calendar days back to search

    Returns:
        List of document dicts.
    """
    scrip_code = get_bse_scrip_code(ticker)
    if not scrip_code:
        print(f"[fetcher] BSE: no scrip code for {ticker} — skipping")
        return []

    to_dt   = datetime.now()
    from_dt = to_dt - timedelta(days=days)

    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    params = {
        "pageno":      1,
        "strCat":      "-1",
        "strPrevDate": from_dt.strftime("%Y%m%d"),
        "strScrip":    scrip_code,
        "strSearch":   "P",
        "strToDate":   to_dt.strftime("%Y%m%d"),
        "strType":     "C",
    }

    try:
        resp = requests.get(url, params=params, headers=_BSE_HEADERS, timeout=15)
        resp.raise_for_status()
        rows = resp.json().get("Table", [])
    except Exception as e:
        print(f"[fetcher] BSE announcements error for {ticker}: {e}")
        return []

    symbol = _normalize_ticker(ticker)
    docs: List[Dict[str, Any]] = []

    for item in rows:
        headline = (item.get("HEADLINE") or "").strip()
        if not headline:
            continue
        attachment = (item.get("ATTACHMENTNAME") or "").strip()
        date_str   = (item.get("NEWS_DT") or "").strip()
        subcatname = (item.get("SUBCATNAME") or "").strip()
        newssub    = (item.get("NEWSSUB") or "").strip()

        # Build a rich text blob
        parts = [f"BSE Corporate Announcement — {headline}"]
        if subcatname:
            parts.append(f"Category: {subcatname}")
        if attachment:
            parts.append(f"Document: {attachment}")
        if newssub:
            parts.append(f"Details: {newssub[:500]}")

        docs.append({
            "text":   "\n".join(parts),
            "source": "BSE",
            "date":   date_str,
            "ticker": symbol,
            "url":    f"https://www.bseindia.com/corporates/ann.html?scripcd={scrip_code}",
            "doc_id": _stable_id("bse", scrip_code, date_str, headline),
        })

    print(f"[fetcher] BSE: {len(docs)} announcements for {ticker}")
    return docs


# ---------------------------------------------------------------------------
# NSE
# ---------------------------------------------------------------------------

def fetch_nse_announcements(ticker: str, days: int = 90) -> List[Dict[str, Any]]:
    """
    Fetch recent corporate announcements from NSE.

    NSE's JSON API requires an active browser session cookie.
    We prime one by visiting the homepage first.
    Falls back gracefully if NSE blocks the request.
    """
    symbol = _normalize_ticker(ticker)

    session = requests.Session()
    session.headers.update({
        **_BROWSER_HEADERS,
        "Accept":          "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://www.nseindia.com/",
    })

    try:
        # Prime session cookie
        session.get("https://www.nseindia.com", timeout=15)
        time.sleep(0.8)

        # NSE changes API paths frequently — try several known endpoints
        nse_endpoints = [
            ("https://www.nseindia.com/api/corp-announcements",
             {"symbol": symbol, "series": "EQ"}),
            ("https://www.nseindia.com/api/corp-info",
             {"symbol": symbol, "type": "announcements"}),
            ("https://www.nseindia.com/api/equity-stockIndices",
             {"index": f"NIFTY%2050"}),          # generic fallback
        ]

        items = []
        for url, params in nse_endpoints:
            try:
                resp = session.get(url, params=params, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict):
                        items = data.get("data", data.get("announcements", []))
                    if items:
                        break
            except Exception:
                continue

    except Exception as e:
        print(f"[fetcher] NSE announcements error for {ticker}: {e}")
        return []

    cutoff = datetime.now() - timedelta(days=days)
    docs: List[Dict[str, Any]] = []

    for item in items:
        desc    = (item.get("desc") or "").strip()
        details = (item.get("an_dt") or "").strip()
        dt_str  = (item.get("an_dt") or "").strip()
        attchmnt = (item.get("attchmnt") or "").strip()

        if not desc:
            continue

        # Date filter
        try:
            item_dt = datetime.strptime(dt_str, "%d-%b-%Y")
            if item_dt < cutoff:
                continue
        except Exception:
            pass  # keep if date can't be parsed

        parts = [f"NSE Corporate Announcement — {desc}"]
        if attchmnt:
            parts.append(f"Document: {attchmnt}")

        docs.append({
            "text":   "\n".join(parts),
            "source": "NSE",
            "date":   dt_str,
            "ticker": symbol,
            "url":    f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}",
            "doc_id": _stable_id("nse", symbol, dt_str, desc),
        })

    print(f"[fetcher] NSE: {len(docs)} announcements for {ticker}")
    return docs


# ---------------------------------------------------------------------------
# RBI
# ---------------------------------------------------------------------------

def _parse_rbi_table(soup: BeautifulSoup, label: str) -> List[Dict[str, Any]]:
    """Parse a standard RBI announcement table from a BeautifulSoup object."""
    docs: List[Dict[str, Any]] = []

    # RBI tables are typically <table class="tablebg"> or id="T1"
    table = soup.find("table", {"id": "T1"}) or soup.find("table", class_="tablebg")
    if not table:
        return docs

    for row in table.find_all("tr")[1:]:          # skip header row
        cols = row.find_all("td")
        if len(cols) < 2:
            continue

        date_str = cols[0].get_text(strip=True)
        title_cell = cols[1]
        title = title_cell.get_text(strip=True)
        if not title:
            continue

        link_tag = title_cell.find("a")
        url = ""
        if link_tag and link_tag.get("href"):
            href = link_tag["href"]
            url = href if href.startswith("http") else "https://www.rbi.org.in" + href

        docs.append({
            "text":   f"{label} — {title}",
            "source": "RBI",
            "date":   date_str,
            "ticker": "RBI",
            "url":    url,
            "doc_id": _stable_id("rbi", label, date_str, title),
        })

    return docs


def fetch_rbi_circulars(days: int = 180, sources_limit: int = 3) -> List[Dict[str, Any]]:
    """
    Fetch recent RBI press releases and regulatory circulars.

    Args:
        days:          How many calendar days back to include.
        sources_limit: Max RBI sources to hit (1=press releases only, 3=all). Default 3.

    Returns:
        List of document dicts tagged with ticker="RBI".
    """
    docs: List[Dict[str, Any]] = []
    headers = {"User-Agent": _BROWSER_HEADERS["User-Agent"]}

    sources = [
        ("https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx", "RBI Press Release"),
        ("https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx", "RBI Circular"),
        ("https://www.rbi.org.in/Scripts/NotificationUser.aspx",        "RBI Notification"),
    ]

    for url, label in sources[:sources_limit]:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            fetched = _parse_rbi_table(soup, label)
            docs.extend(fetched)
            print(f"[fetcher] RBI ({label}): {len(fetched)} items")
        except Exception as e:
            print(f"[fetcher] RBI ({label}) error: {e}")

    return docs


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def fetch_all_filings(ticker: str) -> List[Dict[str, Any]]:
    """
    Fetch all available filings for *ticker* from BSE and all three RBI sources.

    Sources included:
      - BSE: last 30 days of corporate announcements for *ticker*
      - RBI Press Releases: last 90 days (macroeconomic context)
      - RBI Circulars: last 90 days (regulatory guidance)
      - RBI Notifications: last 90 days (banking/policy notices)

    NSE is intentionally skipped — its API requires an active browser session
    cookie and frequently returns 0 results, adding ~15 s latency with no gain.
    fetch_nse_announcements() is implemented but not wired in here.

    Args:
        ticker: Stock ticker string (e.g. "RELIANCE.NS")

    Returns:
        Combined list of document dicts from BSE + RBI (all 3 endpoints).
    """
    print(f"[fetcher] --- Starting fetch for {ticker} ---")

    all_docs: List[Dict[str, Any]] = []

    # BSE: 30 days of corporate announcements
    all_docs.extend(fetch_bse_announcements(ticker, days=30))

    # RBI: press releases, circulars, and notifications (all 3 sources)
    all_docs.extend(fetch_rbi_circulars(days=90, sources_limit=3))

    print(f"[fetcher] --- Total fetched: {len(all_docs)} documents ---")
    return all_docs


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    docs = fetch_all_filings("RELIANCE.NS")
    print(json.dumps(docs[:3], indent=2, default=str))
