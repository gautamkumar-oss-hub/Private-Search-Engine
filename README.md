# Private Search Engine

A privacy-first, local search engine built with FastAPI. It crawls trusted websites, extracts readable HTML, stores a local SQLite FTS5 index, and ranks results with BM25. No accounts, query logs, cookies, or user profiles are used.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000, crawl a trusted domain, and search it. API docs are available at http://localhost:8000/docs.

## API

- `GET /health` - health and privacy status
- `POST /api/crawl` - crawl same-domain HTML pages (`url`, `max_pages`)
- `POST /api/search` - BM25 full-text search (`q`, `limit`)

## Docker

`docker compose up --build` runs the app with a persistent local data volume. The optional `production` profile includes Redis and Meilisearch integration points for scaling cache and retrieval independently.

## Architecture

The index layer is intentionally local-first for a zero-configuration demo. The crawler is same-domain and bounded, preventing accidental broad crawls. The API boundary keeps storage replaceable with PostgreSQL/Meilisearch, while Redis can be added for query caching without changing the frontend contract.
