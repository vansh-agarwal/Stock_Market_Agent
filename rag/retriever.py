"""
Retriever module: freshness checks and semantic search.

Flow
----
1. tools.py calls is_data_fresh(ticker).  If stale, it fetches + ingests
   synchronously via fetch_all_filings() + ingest_documents().
2. search(query, ticker) embeds the query and returns the top-k most
   semantically similar chunks, always mixing in RBI macro documents.

Background-thread machinery (ensure_data / _fetch_and_ingest) was
removed — tools.py owns the fetch lifecycle synchronously to avoid race
conditions between fetch and search.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# Shared singletons from ingest (avoids loading the model twice)
from rag.ingest import _get_collection, _get_model, collection_count
from rag.fetcher import fetch_all_filings
from rag.ingest import ingest_documents

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CACHE_TTL_HOURS = 24   # re-fetch after 24 h
DEFAULT_TOP_K   = 3    # kept low to stay within Groq token-per-minute limits


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------

def _normalize(ticker: str) -> str:
    return ticker.replace(".NS", "").replace(".BO", "").upper()


def is_data_fresh(ticker: str, ttl_hours: int = CACHE_TTL_HOURS) -> bool:
    """
    Return True if ChromaDB contains at least one chunk for *ticker*
    that was ingested within *ttl_hours*.
    """
    try:
        collection = _get_collection()
        if collection.count() == 0:
            return False

        clean = _normalize(ticker)
        results = collection.get(
            where={"ticker": clean},
            limit=1,
            include=["metadatas"],
        )
        metas = results.get("metadatas") or []
        if not metas:
            return False

        ingested_at_str = metas[0].get("ingested_at", "")
        if not ingested_at_str:
            return False

        ingested_at = datetime.fromisoformat(ingested_at_str)
        return datetime.now() - ingested_at < timedelta(hours=ttl_hours)

    except Exception as e:
        print(f"[retriever] freshness check failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------

def search(
    query: str,
    ticker: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
) -> List[Dict[str, Any]]:
    """
    Embed *query* and return the top-k most similar chunks from ChromaDB.

    Args:
        query:  Natural-language search string.
        ticker: Optional ticker to focus results.  RBI documents are always
                included regardless of this filter.
        top_k:  Maximum number of results to return.

    Returns:
        List of dicts with keys: text, source, date, ticker, url, similarity_score.
    """
    collection = _get_collection()
    total      = collection.count()
    if total == 0:
        return []

    model           = _get_model()
    query_embedding = model.encode([query]).tolist()[0]

    # Build where clause: ticker-specific docs + RBI macro docs
    where: Optional[Dict] = None
    if ticker:
        clean = _normalize(ticker)
        where = {"ticker": {"$in": [clean, "RBI"]}}

    n = min(top_k, total)

    try:
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=n,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as first_err:
        # Fallback: try without filter (ChromaDB raises if filter matches 0 docs)
        print(f"[retriever] Filtered query error ({first_err}), retrying without filter...")
        try:
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=n,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            print(f"[retriever] Search failed: {e}")
            return []

    formatted: List[Dict[str, Any]] = []
    docs      = results.get("documents", [[]])[0]
    metas     = results.get("metadatas",  [[]])[0]
    distances = results.get("distances",  [[]])[0]

    for i, doc_text in enumerate(docs):
        meta  = metas[i]     if i < len(metas)     else {}
        dist  = distances[i] if i < len(distances)  else 1.0
        score = round(max(0.0, 1.0 - dist), 4)    # cosine dist -> similarity

        formatted.append({
            "text":             doc_text,
            "source":           meta.get("source", ""),
            "date":             meta.get("date",   ""),
            "ticker":           meta.get("ticker", ""),
            "url":              meta.get("url",    ""),
            "similarity_score": score,
        })

    return formatted


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    ticker = "RELIANCE.NS"
    results = search("quarterly revenue profits", ticker=ticker, top_k=3)
    print(json.dumps(results, indent=2, default=str))

