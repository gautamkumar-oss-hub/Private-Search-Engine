from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"
DB_PATH = DATA_DIR / "search.db"
USER_AGENT = "PrivateSearchEngine/1.0 (privacy-focused research crawler)"


class PageParser(HTMLParser):
    """Extract readable page text and links without executing page scripts."""

    def __init__(self) -> None:
        super().__init__()
        self.title: list[str] = []
        self.text: list[str] = []
        self.links: list[str] = []
        self._in_title = False
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = " ".join(data.split())
        if value:
            self.text.append(value)
            if self._in_title:
                self.title.append(value)


class SearchRequest(BaseModel):
    q: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)


class CrawlRequest(BaseModel):
    url: HttpUrl
    max_pages: int = Field(default=5, ge=1, le=25)


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            crawled_at TEXT NOT NULL,
            content_hash TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            title, content, url UNINDEXED, document_id UNINDEXED
        )
        """
    )
    return connection


def upsert_document(url: str, title: str, content: str) -> None:
    connection = get_connection()
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = connection.execute(
        "SELECT id FROM documents WHERE url = ?", (url,)
    ).fetchone()
    crawled_at = datetime.now(timezone.utc).isoformat()
    if existing:
        connection.execute(
            """
            UPDATE documents
            SET title = ?, content = ?, crawled_at = ?, content_hash = ?
            WHERE id = ?
            """,
            (title, content, crawled_at, digest, existing["id"]),
        )
        connection.execute(
            "DELETE FROM documents_fts WHERE document_id = ?", (str(existing["id"]),)
        )
        document_id = existing["id"]
    else:
        cursor = connection.execute(
            """
            INSERT INTO documents(url, title, content, crawled_at, content_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (url, title, content, crawled_at, digest),
        )
        document_id = cursor.lastrowid
    connection.execute(
        "INSERT INTO documents_fts(title, content, url, document_id) VALUES (?, ?, ?, ?)",
        (title, content, url, str(document_id)),
    )
    connection.commit()
    connection.close()


def search_documents(query: str, limit: int) -> list[dict[str, object]]:
    # FTS5 syntax is deliberately constrained to words to avoid query operators.
    terms = re.findall(r"[A-Za-z0-9_]{2,}", query.lower())
    if not terms:
        return []
    match_query = " AND ".join(f'"{term}"' for term in terms)
    connection = get_connection()
    rows = connection.execute(
        """
        SELECT d.url, d.title, d.content, d.crawled_at, bm25(documents_fts) AS score
        FROM documents_fts
        JOIN documents d ON d.id = CAST(documents_fts.document_id AS INTEGER)
        WHERE documents_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (match_query, limit),
    ).fetchall()
    connection.close()
    return [
        {
            "url": row["url"],
            "title": row["title"],
            "snippet": make_snippet(row["content"], terms),
            "crawled_at": row["crawled_at"],
            "score": round(abs(row["score"]), 4),
        }
        for row in rows
    ]


def make_snippet(content: str, terms: list[str]) -> str:
    words = content.split()
    for index, word in enumerate(words):
        if any(term in word.lower() for term in terms):
            start = max(0, index - 12)
            return " ".join(words[start : start +  thirty_words])
    return " ".join(words[:thirty_words])


thirty_words = 30


def fetch_page(url: str) -> tuple[str, str, list[str]]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=10) as response:
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            raise ValueError("Only HTML pages can be indexed")
        body = response.read(2_000_000).decode(response.headers.get_content_charset() or "utf-8", "replace")
    parser = PageParser()
    parser.feed(body)
    return " ".join(parser.title) or url, " ".join(parser.text), parser.links


def crawl(seed_url: str, max_pages: int) -> int:
    seed = urlparse(seed_url)
    queue = [seed_url]
    seen: set[str] = set()
    indexed = 0
    while queue and indexed < max_pages:
        current = queue.pop(0)
        parsed = urlparse(current)
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
        if normalized in seen or parsed.netloc != seed.netloc:
            continue
        seen.add(normalized)
        try:
            title, content, links = fetch_page(normalized)
            if content.strip():
                upsert_document(normalized, title[:300], content[:100_000])
                indexed += 1
            for link in links:
                candidate = urljoin(normalized, link)
                candidate_parsed = urlparse(candidate)
                if candidate_parsed.scheme in {"http", "https"} and candidate_parsed.netloc == seed.netloc:
                    queue.append(candidate.split("#", 1)[0])
        except (OSError, ValueError, UnicodeError):
            continue
    return indexed


app = FastAPI(title="Private Search Engine", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", response_class=FileResponse, include_in_schema=False)
def index() -> Path:
    return BASE_DIR / "static" / "index.html"


@app.get("/health")
def health() -> dict[str, str]:
    connection = get_connection()
    connection.close()
    return {"status": "ok", "privacy": "no queries or identities are stored"}


@app.post("/api/search")
def search(request: SearchRequest) -> dict[str, object]:
    return {"query": request.q, "results": search_documents(request.q, request.limit)}


@app.post("/api/crawl")
def start_crawl(request: CrawlRequest) -> dict[str, object]:
    try:
        count = crawl(str(request.url), request.max_pages)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"indexed": count, "url": str(request.url)}
