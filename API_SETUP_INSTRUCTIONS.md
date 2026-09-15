# API Key Setup Instructions for Finance Agent

## Required API Keys

To make the Finance Agent fully functional, you need to obtain and configure the following API keys:

### 1. Groq API Key (for the LLM agent)

Groq provides free-tier access to fast LLM inference, which powers the agent's reasoning loop.

- Get it from: https://console.groq.com/
- Click **API Keys** in the left sidebar, then **Create API Key**
- Set as: `GROQ_API_KEY=your_actual_groq_key_here`

The agent uses Groq's Qwen3 models with function-calling support. Without this key the agent cannot run.

### 2. GNews API Key (for financial news)

- Get it from: https://gnews.io/
- Set as: `GNEWS_API_KEY=your_actual_gnews_key_here`

### 3. Marketaux API Key (for financial news)

- Get it from: https://marketaux.com/
- Set as: `MARKETAUX_API_KEY=your_actual_marketaux_key_here`

## Setup Instructions

1. Create a `.env` file in the project root directory (same level as `app.py`)
2. Add the following lines to the `.env` file:

```
GROQ_API_KEY=your_actual_groq_key_here
GNEWS_API_KEY=your_actual_gnews_key_here
MARKETAUX_API_KEY=your_actual_marketaux_key_here
```

3. Replace the placeholder values with your actual API keys
4. Save the file
5. Start the server with `python app.py`; it listens on port 5000 by default (override with the `PORT` env var)

## Important Notes

- **Never commit your `.env` file to version control** — it contains sensitive API keys
- The `.gitignore` file is already configured to exclude `.env` files
- If you accidentally commit your `.env` file, rotate all API keys immediately
- The Groq free tier has token-per-minute rate limits — if you hit them, wait a minute and retry
- The news API keys are optional for basic stock data; they enhance the agent's capabilities
- ChromaDB vector data is stored locally in `data/chroma_db/` and is not committed to git

## Testing Your Setup

After configuring your API keys, start the server and open the web UI:

```bash
python app.py
# Open http://localhost:5000 in your browser
```

You can also check key configuration via the health endpoint:

```bash
curl http://localhost:5000/api/health
# Should return: {"groq_api_key_configured": true, "status": "ok"}
```

## Troubleshooting

If you encounter "API key not valid" errors:
1. Double-check that your API key is correctly copied from https://console.groq.com/
2. Ensure there are no extra spaces or characters in the `.env` file
3. Verify the key is active (not expired or revoked)
4. Check that you are reading `GROQ_API_KEY`, not an old `GEMINI_API_KEY` or `ANTHROPIC_API_KEY`

If you encounter rate limit errors:
1. Wait a minute before retrying — the Groq free tier resets per minute
2. Consider the paid tier if you need sustained higher throughput