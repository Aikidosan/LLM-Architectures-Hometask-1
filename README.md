# GitHub Repository Summarizer

A lightweight Python API service that accepts a public GitHub repository URL and returns a structured, LLM-generated summary of the project — what it does, which technologies it uses, and how it is organized.

## Quick Start

```bash
# 1. Clone or unzip the project
cd github-repo-summarizer

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set your LLM API key (Nebius Token Factory is primary)
export NEBIUS_API_KEY="your_nebius_key_here"
# Alternatively, use OpenAI as a fallback:
# export OPENAI_API_KEY="your_openai_key_here"

# 5. Start the server
uvicorn app:app --host 0.0.0.0 --port 8000
```

The server will be available at `http://localhost:8000`.

## API Reference

### `POST /summarize`

Accepts a GitHub repository URL and returns a structured summary.

**Request**
```json
{
  "github_url": "https://github.com/psf/requests"
}
```

**Success Response (HTTP 200)**
```json
{
  "summary": "**Requests** is a popular Python HTTP library that simplifies making HTTP/1.1 requests. It abstracts away the complexity of urllib3, providing a clean, human-friendly API for GET, POST, and other HTTP methods with support for sessions, authentication, and automatic content decoding.",
  "technologies": ["Python", "urllib3", "certifi", "charset-normalizer", "idna"],
  "structure": "The project follows a standard Python package layout with the main source code in `src/requests/`, tests in `tests/`, and documentation in `docs/`. Configuration files such as `pyproject.toml` and `setup.cfg` are at the root."
}
```

**Error Response**
```json
{
  "status": "error",
  "message": "Repository 'owner/repo' not found. Make sure the URL is correct and the repository is public."
}
```

| HTTP Code | Meaning |
| :--- | :--- |
| 400 | Invalid or malformed GitHub URL, or empty repository |
| 403 | Private repository or GitHub API rate limit exceeded |
| 404 | Repository does not exist |
| 500 | Internal server error or LLM API key not configured |
| 502 | GitHub API or LLM API returned an unexpected error |

**Test with curl**
```bash
curl -X POST http://localhost:8000/summarize \
  -H "Content-Type: application/json" \
  -d '{"github_url": "https://github.com/psf/requests"}'
```

**Interactive docs** are available at `http://localhost:8000/docs` (Swagger UI).

## Environment Variables

| Variable | Required | Description |
| :--- | :--- | :--- |
| `NEBIUS_API_KEY` | Yes (or `OPENAI_API_KEY`) | Nebius Token Factory API key (primary) |
| `OPENAI_API_KEY` | Fallback | OpenAI API key if Nebius is not available |
| `GITHUB_TOKEN` | Optional | GitHub personal access token to raise API rate limits from 60 to 5000 req/hr |

## Design Decisions

### Model Choice

The service uses **`meta-llama/Meta-Llama-3.1-70B-Instruct`** via the Nebius Token Factory API. This model was chosen because it has a large context window (128K tokens), strong instruction-following capability for structured JSON output, and is available at low cost on the Nebius platform. Its 70B parameter scale provides the reasoning depth needed to accurately identify technologies and describe project structure from partial file contents.

### Repository Processing Strategy

The central challenge is that repositories can be arbitrarily large, while LLM context windows are finite and API calls are expensive. The solution uses a five-step pipeline:

**Step 1 — File tree via GitHub Trees API.** A single API call with `?recursive=1` returns the complete file listing without cloning the repository. This is fast, avoids network overhead, and provides file sizes upfront for budget estimation.

**Step 2 — Hard exclusion filter.** Files are excluded if they fall into any of these categories: binary formats (images, fonts, compiled objects, archives), lock files (`package-lock.json`, `yarn.lock`, `poetry.lock`, etc.), dependency directories (`node_modules/`, `vendor/`, `.venv/`, `__pycache__/`), generated output (`dist/`, `build/`), or files larger than 100 KB. These files consume tokens without providing meaningful information about what the project does.

**Step 3 — 3-tier prioritization.** Remaining files are ranked into three tiers before any content is fetched:
- **Tier 1** (highest): `README.md`, `package.json`, `pyproject.toml`, `Dockerfile`, `requirements.txt`, `go.mod`, `Cargo.toml`, and other manifests and config files. These are the most information-dense files in any repository.
- **Tier 2** (medium): All source code files (`.py`, `.js`, `.ts`, `.go`, `.rs`, etc.) not in test or documentation directories.
- **Tier 3** (lowest): Test files, documentation, CI/CD configs, and example code. Useful if budget remains, but not essential for understanding the project.

Within each tier, smaller files are preferred over larger ones, since focused modules tend to be more informative per character than large generated files.

**Step 4 — Budget-aware selection.** File sizes from the tree API are used to estimate how many files will fit within the 100,000 character context budget before any content is downloaded. Only files estimated to fit are fetched, capped at 80 files maximum. This prevents both over-fetching and context overflow.

**Step 5 — Concurrent fetching and assembly.** Selected files are fetched in parallel using `asyncio` with a semaphore of 10 concurrent requests. Each file is truncated to 15,000 characters if needed. The final context is assembled as: directory tree → repository metadata → file contents in tier order.

### Prompt Engineering

The system prompt instructs the LLM to return a strict JSON object with exactly three fields (`summary`, `technologies`, `structure`), includes formatting guidelines for each field, and explicitly forbids markdown code fences in the output. A regex-based fallback strips fences and extracts JSON if the model wraps its response anyway. The temperature is set to 0.2 to reduce hallucination while maintaining fluency.

## Project Structure

```
github-repo-summarizer/
├── app.py            # FastAPI application, request/response models, routing
├── repo_fetcher.py   # GitHub API integration, file filtering, prioritization
├── llm_client.py     # LLM client (Nebius / OpenAI), prompt, JSON parsing
├── requirements.txt  # Python runtime dependencies
└── README.md         # This file
```

## Dependencies

| Package | Version | Purpose |
| :--- | :--- | :--- |
| `fastapi` | 0.115.6 | Web framework and request validation |
| `uvicorn[standard]` | 0.34.0 | ASGI server |
| `httpx` | 0.28.1 | Async HTTP client for GitHub API calls |
| `openai` | 1.58.1 | OpenAI-compatible client (works with Nebius) |
| `pydantic` | 2.10.4 | Data validation and serialization |
