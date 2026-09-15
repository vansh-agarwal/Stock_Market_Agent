"""
Individual tool functions for the Finance Agent
Each tool follows a clean interface with docstrings and schemas for LLM function calling
"""

import yfinance as yf
import json
import math
import requests
import os
from typing import Dict, Any, Optional, List
from datetime import datetime
from dotenv import load_dotenv


def _clean_float(val, decimals: int = 2):
    """Return a rounded float, or None if val is None, NaN, or Inf."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, decimals)
    except (TypeError, ValueError):
        return None


def _format_dividend_yield(raw_yield) -> str:
    """
    yfinance returns dividendYield inconsistently:
    - Some tickers: decimal fraction  (e.g. 0.0046  → 0.46%)
    - Some Indian tickers: already %  (e.g. 1.83    → 1.83%)
    Heuristic: if the raw value > 0.5 it is already a percentage.
    A genuine dividend yield above 50% is essentially impossible.
    """
    if not raw_yield:
        return "0.00%"
    val = float(raw_yield)
    if val > 0.5:          # already in percentage form
        pct = round(val, 2)
    else:                  # decimal form – multiply by 100
        pct = round(val * 100, 2)
    return f"{pct}%"


def get_stock_overview(ticker: str) -> Dict[str, Any]:
    """
    Get comprehensive stock overview data for a given ticker symbol.

    Args:
        ticker (str): Stock ticker symbol (e.g., 'RELIANCE.NS' for NSE, 'RELIANCE.BO' for BSE, 'AAPL' for US)

    Returns:
        Dict[str, Any]: Dictionary containing stock information including:
            - symbol: ticker symbol
            - short_name: company name
            - current_price: latest price
            - market_cap: market capitalization
            - pe_ratio: price-to-earnings ratio
            - dividend_yield: dividend yield percentage
            - 52_week_high: 52 week high price
            - 52_week_low: 52 week low price
            - volume: trading volume
            - avg_volume: average trading volume
            - currency: currency of the stock
            - exchange: exchange where stock is traded
            - last_updated: timestamp of data retrieval

    Example:
        >>> get_stock_overview("RELIANCE.NS")
        {
            "symbol": "RELIANCE.NS",
            "short_name": "Reliance Industries Ltd",
            "current_price": 2450.75,
            ...
        }
    """
    is_indian = ticker.endswith(".NS") or ticker.endswith(".BO")
    base = ticker.replace(".NS", "").replace(".BO", "").upper()

    # Build .NS candidate first, then the correct BSE ticker (scrip_code.BO, not symbol.BO)
    # yfinance uses numeric BSE codes for .BO tickers (e.g. 540376.BO not DMART.BO)
    try:
        from rag.fetcher import get_bse_scrip_code
        bse_code = get_bse_scrip_code(base)
        bse_ticker = f"{bse_code}.BO" if bse_code else f"{base}.BO"
    except Exception:
        bse_code = None
        bse_ticker = f"{base}.BO"

    if not is_indian:
        candidates = [f"{base}.NS", bse_ticker, ticker]
    elif ticker.endswith(".NS"):
        candidates = [ticker, bse_ticker]
    else:
        candidates = [ticker, f"{base}.NS"]

    last_error = None
    for attempt_ticker in candidates:
        try:
            stock = yf.Ticker(attempt_ticker)
            info  = stock.info
            # Use 5d window so weekends/market-closed days don't cause false empty returns
            hist  = stock.history(period="5d")

            if hist.empty:
                last_error = f"No historical data for {attempt_ticker}"
                continue

            # Sanity-check: reject if yfinance resolves to wrong currency region
            # (e.g. bare "TCS" resolves to a US ticker in USD instead of INR)
            currency = info.get("currency", "")
            if not is_indian and currency == "INR":
                pass  # Indian stock found via suffix fallback — good
            if is_indian and currency not in ("INR", ""):
                last_error = f"{attempt_ticker} resolved to non-INR currency ({currency})"
                continue

            latest_data = hist.iloc[-1]

            # --- Current price ---
            # hist.iloc[-1]["Close"] can be NaN when data hasn't settled
            # (common outside trading hours for Indian exchanges on yfinance v1.7+).
            # Prefer info["currentPrice"] / "regularMarketPrice" which are always present
            # for a valid ticker, then fall back to the last non-NaN hist close.
            price = (
                _clean_float(info.get("currentPrice"))
                or _clean_float(info.get("regularMarketPrice"))
            )
            if price is None:
                # Last-resort: scan hist backward for a non-NaN close
                closes = hist["Close"].dropna()
                price = round(float(closes.iloc[-1]), 2) if not closes.empty else None

            overview = {
                "symbol":         attempt_ticker.upper(),
                "short_name":     info.get("shortName", "N/A"),
                "current_price":  price,
                "market_cap":     info.get("marketCap", 0),
                "pe_ratio":       _clean_float(info.get("trailingPE")),
                "dividend_yield": _format_dividend_yield(info.get("dividendYield")),
                "52_week_high":   _clean_float(info.get("fiftyTwoWeekHigh")),
                "52_week_low":    _clean_float(info.get("fiftyTwoWeekLow")),
                "volume":         int(latest_data["Volume"]) if not math.isnan(float(latest_data.get("Volume", float("nan")))) else 0,
                "avg_volume":     info.get("averageVolume", 0),
                "currency":       currency,
                "exchange":       info.get("exchange", "N/A"),
                "last_updated":   datetime.now().isoformat()
            }

            # Replace any remaining None values with "N/A"
            for key, value in overview.items():
                if value is None:
                    overview[key] = "N/A"

            return overview

        except Exception as e:
            last_error = str(e)
            continue

    return {
        "error": f"Failed to retrieve stock data for {ticker}: {last_error}",
        "symbol": ticker.upper(),
        "last_updated": datetime.now().isoformat()
    }

def get_news(ticker: str, limit: int = 10) -> Dict[str, Any]:
    """
    Get recent news articles for a given stock ticker from GNews and Marketaux APIs.

    Args:
        ticker (str): Stock ticker symbol (e.g., 'RELIANCE.NS' for NSE)
        limit (int): Maximum number of news articles to return per source

    Returns:
        Dict[str, Any]: News articles data from both sources combined
    """
    try:
        is_indian = ticker.endswith(".NS") or ticker.endswith(".BO")
        clean_ticker = ticker.replace('.NS', '').replace('.BO', '')

        # Resolve proper company name from yfinance, validating currency for Indian stocks
        company_name = None
        candidates = [ticker] if is_indian else [clean_ticker + ".NS", clean_ticker + ".BO", ticker]

        for t in candidates:
            try:
                info = yf.Ticker(t).info
                name = info.get("shortName") or info.get("longName")
                currency = info.get("currency", "")
                if name and (currency == "INR" or not is_indian):
                    company_name = name
                    break
            except Exception:
                continue

        # Build GNews search query — always set, no fragile dir() check
        if not company_name:
            company_name = clean_ticker + (" India" if is_indian else "")
            gnews_query = company_name
        elif is_indian and "india" not in company_name.lower():
            gnews_query = company_name + " India"
        else:
            gnews_query = company_name

        all_articles = []

        # GNews: use country filter based on market — avoids mismatch (e.g. TCS → Container Store)
        gnews_articles = _fetch_gnews(gnews_query, limit, country="in" if is_indian else None)
        if gnews_articles:
            all_articles.extend(gnews_articles)

        # Marketaux: scoped to ticker symbol directly, no disambiguation needed
        marketaaux_articles = _fetch_marketaaux(clean_ticker, limit)
        if marketaaux_articles:
            all_articles.extend(marketaaux_articles)

        unique_articles = _remove_duplicate_articles(all_articles)
        unique_articles.sort(key=lambda x: x.get('publishedAt', ''), reverse=True)
        final_articles = unique_articles[:limit]

        return {
            "ticker": ticker,
            "company_name": company_name,
            "articles_found": len(final_articles),
            "articles": final_articles,
            "sources": ["GNews", "Marketaux"],
            "last_updated": datetime.now().isoformat()
        }

    except Exception as e:
        return {
            "error": f"Failed to retrieve news for {ticker}: {str(e)}",
            "ticker": ticker,
            "last_updated": datetime.now().isoformat()
        }






def _fetch_gnews(company_name: str, limit: int, country: str = None) -> List[Dict[str, Any]]:
    """Fetch news from GNews API."""
    try:
        api_key = os.getenv("GNEWS_API_KEY")
        if not api_key:
            return []

        url = "https://gnews.io/api/v4/search"
        params = {
            "q": company_name,
            "lang": "en",
            "max": min(limit, 10),
            "apikey": api_key
        }
        if country:
            params["country"] = country  # e.g. "in" for Indian stocks only

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        articles = []
        for article in data.get("articles", []):
            articles.append({
                "title":       article.get("title", ""),
                "description": article.get("description", ""),
                "content":     article.get("content", ""),
                "url":         article.get("url", ""),
                "source":      article.get("source", {}).get("name", "GNews"),
                "publishedAt": article.get("publishedAt", ""),
                "image":       article.get("image", ""),
                "source_api":  "GNews"
            })

        return articles

    except Exception as e:
        print(f"GNews API error: {e}")
        return []


def _fetch_marketaaux(ticker: str, limit: int) -> List[Dict[str, Any]]:
    """Fetch news from Marketaux API."""
    try:
        api_key = os.getenv("MARKETAUX_API_KEY")
        if not api_key:
            return []

        url = "https://api.marketaux.com/v1/news/all"
        params = {
            "symbols": ticker,
            "filter_entities": "true",
            "limit": min(limit, 50),  # Marketaux allows up to 50
            "api_token": api_key
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        articles = []
        for article in data.get("data", []):
            entities = article.get("entities", [])
            # Find the matching ticker entity
            ticker_entity = None
            for entity in entities:
                if entity.get("symbol") == ticker:
                    ticker_entity = entity
                    break

            articles.append({
                "title": article.get("title", ""),
                "description": article.get("description", ""),
                "content": article.get("snippet", ""),
                "url": article.get("url", ""),
                "source": article.get("source", ""),
                "publishedAt": article.get("published_at", ""),
                "image": article.get("image_url", ""),
                "source_api": "Marketaux",
                "entities": entities,
                "relevance_score": ticker_entity.get("score", 0) if ticker_entity else 0
            })

        return articles

    except Exception as e:
        print(f"Marketaux API error: {e}")
        return []


def _remove_duplicate_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove duplicate articles based on title similarity."""
    if not articles:
        return []

    unique_articles = []
    seen_titles = set()

    for article in articles:
        title = article.get("title", "").lower().strip()
        # Simple deduplication: if title is very similar, skip
        if title and title not in seen_titles:
            seen_titles.add(title)
            unique_articles.append(article)

    return unique_articles


def query_filings_rag(query: str, ticker: str = None) -> Dict[str, Any]:
    """
    Query the RAG pipeline for NSE/BSE/RBI regulatory filings and documents.

    Automatically fetches live filings from BSE, NSE and RBI for the given
    ticker (or refreshes the 24-hour cache) before performing a semantic search.

    Args:
        query (str): Natural-language search query
        ticker (str, optional): Stock ticker (e.g. 'RELIANCE.NS') to focus results.
                                RBI macro documents are always included.

    Returns:
        Dict[str, Any]: {
            query:          original query string,
            ticker:         ticker filter used (if any),
            results_count:  number of chunks returned,
            results:        list of { text, source, date, ticker, url, similarity_score },
            note:           informational message
        }
    """
    try:
        from rag.retriever import is_data_fresh, search
        from rag.fetcher import fetch_all_filings
        from rag.ingest import ingest_documents

        note_parts = []

        # SYNCHRONOUS fetch+ingest when cache is stale or missing.
        # Previously this used a background thread (ensure_data) and returned
        # immediately, causing search() to always run on empty/stale data.
        if ticker:
            if not is_data_fresh(ticker):
                print(f"[query_filings_rag] Cache miss for {ticker} — fetching synchronously...")
                try:
                    docs = fetch_all_filings(ticker)
                    if docs:
                        ingest_documents(docs)
                        note_parts.append(f"Fetched {len(docs)} fresh filings for {ticker}.")
                    else:
                        note_parts.append(f"No filings found for {ticker} on BSE/RBI.")
                except Exception as fetch_err:
                    print(f"[query_filings_rag] Fetch failed for {ticker}: {fetch_err}")
                    note_parts.append(f"Live fetch failed ({fetch_err}); searching cached data.")
            else:
                note_parts.append(f"Using cached filings for {ticker} (refreshed within 24h).")

        results = search(query, ticker=ticker)

        if not results:
            return {
                "query":         query,
                "ticker":        ticker,
                "results_count": 0,
                "results":       [],
                "note": (
                    "No relevant documents found. "
                    "Try a broader query or check that the ticker is listed on NSE/BSE."
                ),
            }

        note_parts.append("Results from BSE/NSE corporate filings and RBI documents. Data cached for 24h.")
        return {
            "query":         query,
            "ticker":        ticker,
            "results_count": len(results),
            "results":       results,
            "note":          " ".join(note_parts),
        }

    except Exception as e:
        return {
            "error":  f"RAG pipeline error: {str(e)}",
            "query":  query,
            "ticker": ticker,
        }


if __name__ == "__main__":
    # Test the stock overview function
    print("Testing get_stock_overview with RELIANCE.NS:")
    result = get_stock_overview("RELIANCE.NS")
    print(json.dumps(result, indent=2))