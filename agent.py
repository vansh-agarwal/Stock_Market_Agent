"""
Orchestration loop for the Finance Agent using Groq API (free tier).
Implements a ReAct-style loop with OpenAI-compatible tool calling.
Includes exponential backoff retry logic for rate limit and overload errors.
"""

import os
import json
import sys
import time
import random
from typing import Dict, Any, List, Optional
from groq import Groq, RateLimitError, APIStatusError, APIConnectionError
from dotenv import load_dotenv
from tools import get_stock_overview, get_news, query_filings_rag

# Load environment variables
load_dotenv()

# Initialize Groq client
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Models to try in order of preference (confirmed tool calling support)
MODELS_TO_TRY = [
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
]

# System prompt for the finance agent
SYSTEM_PROMPT = """You are a specialized financial research agent for Indian and global markets.
Your goal is to provide comprehensive investment research by using available tools to gather
stock data, news, and regulatory filings.

When answering:
1. Start by understanding what information is needed
2. Use tools strategically to gather relevant data
3. Synthesize information from multiple sources
4. Provide clear, well-sourced answers
5. If you need more information, continue using tools
6. Always cite your sources from the tool outputs

Available tools:
- get_stock_overview: Get current stock data and metrics
- get_news: Get recent financial news
- query_filings_rag: Search regulatory documents and filings

Think step by step and use tools as needed to answer the user's question comprehensively.

IMPORTANT DATA FORMATTING RULES:
- The `dividend_yield` field from get_stock_overview is pre-formatted as a percentage string (e.g., "0.46%"). Use it exactly as returned — do NOT multiply or reformat it.
- Market cap is in raw numbers (e.g., 17520000000000 = ₹17.52 Lakh Crore). Format for readability.
- Do not use ~~strikethrough~~ formatting in your responses.

RESPONSE STYLE RULES:
- Do NOT create a section headed "Data Availability Note", "Limitation", "Recommendation", or "Key Takeaways" unless the user explicitly requested one. If data is unavailable, state it in a single plain sentence within the relevant paragraph and continue.
- Do NOT prefix section headers with warning symbols or emojis (e.g. ⚠️, 🔴, ℹ️). Use plain text headers only.
- Do NOT end your response with a follow-up offer such as "Would you like me to...", "Shall I also pull...", or "If the data feed recovers, I can re-run...". Give a complete, self-contained answer and stop.
- Do NOT add disclaimers like "This is not financial advice" unless the user specifically asks.
- Do NOT append a "Note:" callout block at the end of your response."""

# Tool definitions in OpenAI/Groq format
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_overview",
            "description": "Get comprehensive stock overview data for a given ticker symbol including price, market cap, P/E ratio, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Stock ticker symbol (e.g., 'RELIANCE.NS' for NSE, 'RELIANCE.BO' for BSE, 'AAPL' for US)"
                    }
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Get recent news articles for a given stock ticker from financial news sources",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Stock ticker symbol"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of news articles to return (default: 10)"
                    }
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_filings_rag",
            "description": "Query the RAG pipeline for NSE/BSE/RBI regulatory filings and documents",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for relevant documents"
                    },
                    "ticker": {
                        "type": "string",
                        "description": "Optional: Filter results by specific ticker symbol"
                    }
                },
                "required": ["query"]
            }
        }
    }
]

# Retry configuration
MAX_RETRIES = 4
BASE_DELAY = 5
MAX_DELAY = 60


def _is_rate_limit_error(error: Exception) -> bool:
    """Check if the error is a rate limit error (429)."""
    if isinstance(error, RateLimitError):
        return True
    return "429" in str(error) or "rate_limit" in str(error).lower()


def _is_overload_error(error: Exception) -> bool:
    """Check if the error is a service overload error (503)."""
    if isinstance(error, APIStatusError) and error.status_code in (503, 529):
        return True
    return "503" in str(error) or "overloaded" in str(error).lower()


def _is_retryable_error(error: Exception) -> bool:
    """Check if the error is worth retrying."""
    return _is_rate_limit_error(error) or _is_overload_error(error)


def _is_model_unavailable(error: Exception) -> bool:
    """Check if this specific model is unavailable / not found."""
    if isinstance(error, APIStatusError) and error.status_code == 404:
        return True
    err = str(error).lower()
    return "not found" in err or "model_not_found" in err or "404" in str(error)


def _trim_tool_result(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Strip heavy / redundant fields from tool results before sending to the LLM.
    Keeps the information the LLM needs while minimising input tokens.
    """
    if tool_name == "get_news":
        articles = result.get("articles", [])
        trimmed = []
        for a in articles[:6]:   # cap at 6 articles
            trimmed.append({
                "title":       a.get("title", ""),
                "description": a.get("description", "")[:150],  # keep summary for synthesis
                "source":      a.get("source", ""),
                "publishedAt": a.get("publishedAt", ""),
                "url":         a.get("url", ""),
            })
        return {
            "ticker":         result.get("ticker"),
            "company_name":   result.get("company_name"),
            "articles_found": result.get("articles_found"),
            "articles":       trimmed,
        }

    if tool_name == "query_filings_rag":
        results = result.get("results", [])
        trimmed = []
        for r in results:
            trimmed.append({
                "text":   r.get("text", "")[:250],   # cap chunk at 250 chars
                "source": r.get("source", ""),
                "date":   r.get("date", ""),
                "ticker": r.get("ticker", ""),
            })
        return {
            "query":         result.get("query"),
            "ticker":        result.get("ticker"),
            "results_count": result.get("results_count"),
            "results":       trimmed,
            "note":          result.get("note", "")[:200],
        }

    return result


def execute_tool(tool_name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool function with the given input."""
    TOOL_FUNCTIONS = {
        "get_stock_overview": get_stock_overview,
        "get_news": get_news,
        "query_filings_rag": query_filings_rag,
    }

    if tool_name not in TOOL_FUNCTIONS:
        return {"error": f"Unknown tool: {tool_name}"}

    try:
        return TOOL_FUNCTIONS[tool_name](**tool_input)
    except Exception as e:
        return {"error": f"Tool execution failed: {str(e)}"}


def _call_groq_with_retry(
    model: str,
    messages: List[Dict],
    label: str = "API call"
) -> Any:
    """
    Call Groq API with exponential backoff on rate limit / overload errors.
    Raises on non-retryable errors or after MAX_RETRIES exhausted.
    """
    delay = BASE_DELAY

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=8192,
                temperature=0.1,
            )
        except Exception as e:
            if _is_retryable_error(e):
                if attempt == MAX_RETRIES:
                    print(f"[retry] {label} failed after {MAX_RETRIES} retries. Giving up.")
                    raise

                jitter = random.uniform(0, delay * 0.3)
                wait = min(delay + jitter, MAX_DELAY)
                reason = "rate limit (429)" if _is_rate_limit_error(e) else "overloaded (503)"
                print(f"[retry] {label} — {reason} (attempt {attempt}/{MAX_RETRIES}). "
                      f"Waiting {wait:.1f}s before retry…")
                time.sleep(wait)
                delay = min(delay * 2, MAX_DELAY)
            else:
                raise


def run_agent_loop(user_query: str, max_iterations: int = 10, ticker: str = "") -> str:
    """
    Run the ReAct agent loop using Groq with tool calling.

    Args:
        user_query (str): The user's question or request
        max_iterations (int): Maximum number of tool call iterations
        ticker (str): Optional stock ticker pre-extracted from the UI (e.g. "RELIANCE.NS").
                      When provided it is injected into the user message so the LLM and all
                      tool calls reliably target the right stock without relying on free-text
                      extraction.  Falls back to LLM-inference when empty.

    Returns:
        str: Final response from the agent
    """
    # Pick a working model from the list
    active_model: Optional[str] = None
    for model_name in MODELS_TO_TRY:
        try:
            # Probe with a tiny request to validate model availability
            client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=5,
            )
            active_model = model_name
            print(f"[agent] Using model: {active_model}")
            break
        except Exception as e:
            if _is_model_unavailable(e):
                print(f"[agent] Model {model_name} not found, trying next…")
                continue
            elif _is_retryable_error(e):
                print(f"[agent] Model {model_name} overloaded, trying next…")
                continue
            else:
                # Use it anyway — the real call may still work
                active_model = model_name
                print(f"[agent] Using model: {active_model} (probe failed: {e})")
                break

    if active_model is None:
        return (
            " No Groq models are currently available. "
            "Please wait a moment and try again."
        )

    # Build the user message, prepending ticker context when available
    if ticker:
        user_message = (
            f"[Context: the user is asking about ticker symbol {ticker}]\n\n"
            f"{user_query}"
        )
    else:
        user_message = user_query

    # Build conversation messages
    messages: List[Dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    for iteration in range(max_iterations):
        try:
            response = _call_groq_with_retry(
                active_model,
                messages,
                label=f"Groq call (iteration {iteration + 1})"
            )
        except Exception as e:
            if _is_retryable_error(e):
                reason = "rate limit" if _is_rate_limit_error(e) else "service overload"
                return (
                    f" Groq API {reason} — retried {MAX_RETRIES} times but persists. "
                    "Please wait a few minutes and try again."
                )
            return f"Error calling Groq: {str(e)}"

        choice = response.choices[0]
        message = choice.message

        # Add assistant message to history
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    }
                }
                for tc in (message.tool_calls or [])
            ] or None
        })

        # If the model wants to call tools
        if choice.finish_reason == "tool_calls" and message.tool_calls:
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                try:
                    tool_input = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    tool_input = {}

                # If a ticker was provided by the frontend and this tool accepts one,
                # inject it as a default when the LLM omitted it.
                if ticker:
                    tools_with_ticker = {"get_stock_overview", "get_news", "query_filings_rag"}
                    if tool_name in tools_with_ticker and "ticker" not in tool_input:
                        tool_input["ticker"] = ticker

                print(f"[agent] Calling tool: {tool_name} with input: {tool_input}")
                result = execute_tool(tool_name, tool_input)

                # Trim heavy fields before sending to the LLM to reduce input tokens
                result = _trim_tool_result(tool_name, result)

                # Send tool result back
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, default=str),
                })
            continue

        # No more tool calls — return the final answer
        if message.content:
            return message.content

        return "Agent completed without producing a response."

    return (
        f"Agent reached maximum iterations ({max_iterations}) without completing. "
        "Consider simplifying your query."
    )


# For testing directly
if __name__ == "__main__":
    test_query = "What's RELIANCE.NS trading at right now?"
    print("Testing agent loop with query:", test_query)
    print("=" * 50)
    result = run_agent_loop(test_query)
    print("Final Answer:")
    try:
        print(result)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(result.encode("utf-8") + b"\n")
    print("=" * 50)