import os
import uuid
import json
import threading
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from agent import run_agent_loop

load_dotenv()

# Allowed upload extensions
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".xlsx", ".csv"}
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _prewarm():
    """Load the embedding model and ChromaDB into memory at startup."""
    try:
        from rag.ingest import _get_model, _get_collection
        _get_collection()
        _get_model()
        print("[startup] Embedding model + ChromaDB pre-warmed.")
    except Exception as e:
        print(f"[startup] Pre-warm failed (non-fatal): {e}")

threading.Thread(target=_prewarm, daemon=True).start()

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)  # Allow cross-origin requests during development

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
def index():
    """Serve the main HTML frontend."""
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """
    Run the finance agent on a user query.

    Request body (JSON):
        query (str): The user's investment question
        ticker (str, optional): Stock ticker symbol (already formatted, e.g. RELIANCE.NS)

    Returns:
        JSON with 'answer' field containing the agent's response.
    """
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        query = data.get("query", "").strip()
        if not query:
            return jsonify({"error": "Field 'query' is required and cannot be empty"}), 400

        # Extract optional ticker and pass it through to the agent
        ticker = data.get("ticker", "").strip()

        # Run the agent
        answer = run_agent_loop(query, ticker=ticker)

        return jsonify({"answer": answer, "query": query})

    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


@app.route("/api/upload-filings", methods=["POST"])
def upload_filings():
    """
    Accept one or more NSE filing files, parse them and ingest into ChromaDB.

    Form fields:
        files   : one or more uploaded files (PDF, DOCX, TXT, XLSX, CSV)
        ticker  : (optional) stock ticker to tag documents with (e.g. RELIANCE.NS)

    Returns JSON:
        {
            "files_processed": int,
            "chunks_ingested": int,
            "skipped":         [list of skipped filenames with reasons],
            "ingested_files":  [list of successfully processed filenames]
        }
    """
    from rag.parser import extract_text
    from rag.ingest import ingest_documents

    if "files" not in request.files:
        return jsonify({"error": "No files provided. Send files under the 'files' field."}), 400

    files = request.files.getlist("files")
    ticker = (request.form.get("ticker") or "").strip().upper() or "UPLOADED"
    source_label = "NSE_UPLOAD"

    ingested_files = []
    skipped = []
    docs = []

    for f in files:
        original_name = f.filename or "unknown"
        ext = os.path.splitext(original_name)[1].lower()

        if ext not in ALLOWED_EXTENSIONS:
            skipped.append({
                "filename": original_name,
                "reason": f"Unsupported file type '{ext}'. Allowed: PDF, DOCX, TXT, XLSX, CSV"
            })
            continue

        # Save to a temporary path
        safe_name = f"{uuid.uuid4().hex}{ext}"
        save_path = os.path.join(UPLOAD_DIR, safe_name)
        try:
            f.save(save_path)
        except Exception as e:
            skipped.append({"filename": original_name, "reason": f"Failed to save: {e}"})
            continue

        # Extract text
        try:
            text = extract_text(save_path, filename=original_name)
        except Exception as e:
            skipped.append({"filename": original_name, "reason": f"Parse error: {e}"})
            os.remove(save_path)
            continue

        if not text or not text.strip():
            skipped.append({"filename": original_name, "reason": "No text could be extracted from this file."})
            os.remove(save_path)
            continue

        # Build a doc dict for ingest
        from datetime import datetime
        docs.append({
            "text":   text,
            "doc_id": f"upload_{uuid.uuid4().hex}",
            "source": source_label,
            "date":   datetime.now().strftime("%Y-%m-%d"),
            "ticker": ticker,
            "url":    f"uploaded://{original_name}",
        })
        ingested_files.append(original_name)

        # Remove temp file after reading
        try:
            os.remove(save_path)
        except Exception:
            pass

    # Ingest all parsed docs at once
    chunks_ingested = 0
    if docs:
        try:
            chunks_ingested = ingest_documents(docs)
        except Exception as e:
            return jsonify({"error": f"Ingest failed: {str(e)}"}), 500

    return jsonify({
        "files_processed": len(ingested_files),
        "chunks_ingested":  chunks_ingested,
        "ingested_files":   ingested_files,
        "skipped":          skipped,
    })


@app.route("/api/filings-status", methods=["GET"])
def filings_status():
    """Return the total number of document chunks currently in ChromaDB."""
    try:
        from rag.ingest import collection_count
        count = collection_count()
        return jsonify({"total_chunks": count, "status": "ok"})
    except Exception as e:
        return jsonify({"total_chunks": 0, "status": "error", "error": str(e)})


@app.route("/api/health", methods=["GET"])
def health():
    """Health check endpoint."""
    api_key_set = bool(os.getenv("GROQ_API_KEY"))
    return jsonify({
        "status": "ok",
        "groq_api_key_configured": api_key_set
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"Starting Finance Agent server on http://localhost:{port}")
    print("Press Ctrl+C to stop")
    app.run(host="0.0.0.0", port=port, debug=False)
