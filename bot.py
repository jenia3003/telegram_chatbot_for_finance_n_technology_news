"""Telegram chatbot with RSS news and optional Gemini responses."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # The bot can still collect articles without Gemini installed.
    genai = None
    genai_types = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_BATCH_SIZE = max(1, min(10, int(os.getenv("GEMINI_BATCH_SIZE", "8"))))
GEMINI_EXCERPT_CHARS = max(
    600,
    min(3000, int(os.getenv("GEMINI_EXCERPT_CHARS", "3000"))),
)
DB_PATH = os.getenv("DB_PATH", "bot_news.db")
SOURCES_PATH = Path(os.getenv("SOURCES_PATH", str(Path(__file__).with_name("sources.json"))))
SOURCE_CATALOG_PATH = Path(
    os.getenv("SOURCE_CATALOG_PATH", str(Path(__file__).with_name("source_catalog.json")))
)
MIN_RELIABILITY_SCORE = int(os.getenv("MIN_RELIABILITY_SCORE", "7"))
if not 1 <= MIN_RELIABILITY_SCORE <= 10:
    raise ValueError("MIN_RELIABILITY_SCORE must be between 1 and 10")
DATABASE_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
DISCOVERY_ENABLED = os.getenv("DISCOVERY_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DISCOVERY_MAX_ITEMS = max(1, int(os.getenv("DISCOVERY_MAX_ITEMS", "6")))


# ---------------------------------------------------------------------------
# Shared metadata and formatting helpers
# ---------------------------------------------------------------------------

def format_db_datetime(value: datetime) -> str:
    """Format persisted timestamps as readable Vietnam-local time with offset."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(DATABASE_TIMEZONE).isoformat(
        sep=" ",
        timespec="seconds",
    )


# These groups add context to job-market snapshots before they are summarized.
JOB_ROLE_SKILLS = {
    "Data Analyst / BI": ["SQL", "Excel", "Power BI/Tableau", "thống kê", "trình bày insight"],
    "Data Engineer / Analytics Engineer": [
        "Python/SQL",
        "ETL/ELT",
        "data warehouse",
        "orchestration",
        "cloud",
    ],
    "Data Scientist / ML / AI": [
        "Python",
        "xác suất-thống kê",
        "machine learning",
        "thí nghiệm",
        "triển khai mô hình",
    ],
    "Product / Business Analyst": [
        "SQL",
        "phân tích KPI",
        "tư duy sản phẩm",
        "thu thập yêu cầu",
        "giao tiếp stakeholder",
    ],
}


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

class VisibleTextParser(HTMLParser):
    """Extract readable text while ignoring scripts and page chrome code."""

    ignored_tags = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.ignored_tags:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.ignored_tags and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            text = re.sub(r"\s+", " ", data).strip()
            if text:
                self.parts.append(text)


def extract_visible_text(html_content: bytes) -> str:
    """Return visible page text while discarding scripts and layout noise."""
    parser = VisibleTextParser()
    parser.feed(html_content.decode("utf-8", errors="ignore"))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


# ---------------------------------------------------------------------------
# External service clients
# ---------------------------------------------------------------------------

# Keep the client optional so collection and database operations still work
# when the Gemini key is not configured.
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if genai and GEMINI_API_KEY else None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Create the local article store if it does not exist yet."""
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER NOT NULL UNIQUE,
                url_hash TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source TEXT,
                url TEXT,
                category TEXT,
                summary TEXT,
                published_at TEXT,
                sent_to_user INTEGER NOT NULL DEFAULT 0
                    CHECK (sent_to_user IN (0, 1)),
                ready_to_send INTEGER NOT NULL DEFAULT 0
                    CHECK (ready_to_send IN (0, 1))
            )
            """
        )
        # Apply small, additive migrations so an existing local database keeps
        # working after the bot is upgraded.
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(articles)").fetchall()
        }
        if "ready_to_send" not in columns:
            connection.execute(
                "ALTER TABLE articles ADD COLUMN ready_to_send INTEGER NOT NULL DEFAULT 0"
            )
        if "url" not in columns:
            connection.execute("ALTER TABLE articles ADD COLUMN url TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS discovery_candidates (
                domain TEXT PRIMARY KEY,
                sample_url TEXT NOT NULL,
                source_name TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'candidate'
                    CHECK (status IN ('candidate', 'approved', 'rejected'))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_checkpoints (
                chat_id INTEGER NOT NULL,
                scope TEXT NOT NULL,
                last_checked_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, scope)
            )
            """
        )
        # Normalize timestamps once when the database is opened.
        migrate_database_timestamps(connection)
        connection.commit()


def get_chat_checkpoint(chat_id: int, scope: str) -> datetime | None:
    """Return the last successful on-demand check for a chat and command scope."""
    with closing(sqlite3.connect(DB_PATH)) as connection:
        row = connection.execute(
            """
            SELECT last_checked_at
            FROM chat_checkpoints
            WHERE chat_id = ? AND scope = ?
            """,
            (chat_id, scope),
        ).fetchone()
    return parse_published_at(row[0]) if row else None


def save_chat_checkpoint(chat_id: int, scope: str, checked_at: datetime) -> None:
    """Persist a successful on-demand check timestamp."""
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            """
            INSERT INTO chat_checkpoints (chat_id, scope, last_checked_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, scope) DO UPDATE SET
                last_checked_at = excluded.last_checked_at
            """,
            (chat_id, scope, format_db_datetime(checked_at)),
        )
        connection.commit()


def entry_url_hash(entry: dict) -> str:
    """Return the required SHA-256 hash for an article URL."""
    url = (entry.get("link") or "").strip()
    return hashlib.sha256(url.encode()).hexdigest() if url else ""


def record_discovery_candidates(candidates: list[dict]) -> None:
    """Keep newly observed, unapproved domains for explicit catalog review."""
    if not candidates:
        return
    now = format_db_datetime(datetime.now(timezone.utc))
    with closing(sqlite3.connect(DB_PATH)) as connection:
        for candidate in candidates:
            domain = candidate.get("domain", "").strip().lower()
            sample_url = candidate.get("sample_url", "").strip()
            if not domain or not sample_url:
                continue
            connection.execute(
                """
                INSERT INTO discovery_candidates
                    (domain, sample_url, source_name, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    sample_url = excluded.sample_url,
                    source_name = excluded.source_name,
                    last_seen_at = excluded.last_seen_at,
                    hit_count = discovery_candidates.hit_count + 1
                """,
                (domain, sample_url, candidate.get("source_name", ""), now, now),
            )
        connection.commit()


def save_entries(entries: list[dict]) -> list[str]:
    """Insert RSS entries only when their URL hash is not already stored."""
    saved_hashes: list[str] = []
    with closing(sqlite3.connect(DB_PATH)) as connection:
        hashes = [entry_url_hash(entry) for entry in entries]
        hashes = [article_hash for article_hash in hashes if article_hash]
        if hashes:
            placeholders = ", ".join("?" for _ in hashes)
            existing_hashes = {
                row[0]
                for row in connection.execute(
                    f"SELECT url_hash FROM articles WHERE url_hash IN ({placeholders})",
                    hashes,
                ).fetchall()
            }
        else:
            existing_hashes = set()

        for entry in entries:
            article_hash = entry_url_hash(entry)
            if not article_hash:
                continue
            saved_hashes.append(article_hash)
            if article_hash in existing_hashes:
                connection.execute(
                    "UPDATE articles SET url = COALESCE(NULLIF(?, ''), url) "
                    "WHERE url_hash = ?",
                    (entry.get("link", "").strip(), article_hash),
                )
                continue
            published_at = parse_published_at(entry.get("_published_at"))
            connection.execute(
                """
                INSERT INTO articles
                    (id, url_hash, title, source, url, category, summary, published_at)
                VALUES
                    ((SELECT COALESCE(MAX(id), 0) + 1 FROM articles), ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article_hash,
                    entry.get("title", "").strip(),
                    entry.get("_source", "RSS"),
                    entry.get("link", "").strip(),
                    entry.get("_category", ""),
                    entry.get("summary", "").strip(),
                    format_db_datetime(published_at or datetime.now(timezone.utc)),
                ),
            )
            existing_hashes.add(article_hash)
        connection.commit()
    return saved_hashes


def mark_entries_sent(article_hashes: list[str]) -> None:
    """Mark articles as delivered after Telegram accepts the message."""
    if not article_hashes:
        return
    placeholders = ", ".join("?" for _ in article_hashes)
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            f"UPDATE articles SET sent_to_user = 1 WHERE url_hash IN ({placeholders})",
            article_hashes,
        )
        connection.commit()


def get_article_analysis(article_hashes: list[str]) -> dict[str, dict]:
    """Load stored Gemini results keyed by each article's URL hash."""
    if not article_hashes:
        return {}
    placeholders = ", ".join("?" for _ in article_hashes)
    with closing(sqlite3.connect(DB_PATH)) as connection:
        rows = connection.execute(
            f"SELECT url_hash, category, summary, ready_to_send "
            f"FROM articles WHERE url_hash IN ({placeholders})",
            article_hashes,
        ).fetchall()
    return {
        row[0]: {
            "category": row[1],
            "summary": row[2],
            "ready_to_send": bool(row[3]),
        }
        for row in rows
    }


def parse_published_at(value: str | datetime | None) -> datetime | None:
    """Parse common RSS date formats and normalize them to UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = " ".join(value.replace("\u202f", " ").split())
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(normalized)
            except (TypeError, ValueError, OverflowError):
                parsed = None
            if parsed is None:
                for date_format in (
                    "%m/%d/%Y %I:%M:%S %p",
                    "%m/%d/%Y %I:%M %p",
                ):
                    try:
                        parsed = datetime.strptime(normalized, date_format)
                        break
                    except ValueError:
                        continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def migrate_database_timestamps(connection: sqlite3.Connection) -> None:
    """Rewrite legacy timestamps into readable Vietnam-local ISO values."""
    timestamp_columns = (
        ("articles", "url_hash", "published_at"),
        ("discovery_candidates", "domain", "first_seen_at"),
        ("discovery_candidates", "domain", "last_seen_at"),
        ("chat_checkpoints", "chat_id || '|' || scope", "last_checked_at"),
    )
    for table, key_expression, column in timestamp_columns:
        rows = connection.execute(
            f"SELECT {key_expression}, {column} FROM {table} "
            f"WHERE {column} IS NOT NULL"
        ).fetchall()
        for key, raw_value in rows:
            parsed = parse_published_at(raw_value)
            if parsed is None:
                continue
            normalized = format_db_datetime(parsed)
            if normalized == raw_value:
                continue
            if table == "chat_checkpoints":
                chat_id, scope = str(key).split("|", 1)
                connection.execute(
                    "UPDATE chat_checkpoints SET last_checked_at = ? "
                    "WHERE chat_id = ? AND scope = ?",
                    (normalized, chat_id, scope),
                )
            else:
                key_column = "url_hash" if table == "articles" else (
                    "domain" if table == "discovery_candidates" else "chat_id"
                )
                connection.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {key_column} = ?",
                    (normalized, key),
                )


def get_recent_ready_articles(
    category: str | None = None,
    hours: int = 24,
    limit: int = 10,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Read analyzed, sendable articles in an absolute or relative time window."""
    where_category = " AND category = ?" if category else ""
    parameters: list[str | int] = [category] if category else []
    with closing(sqlite3.connect(DB_PATH)) as connection:
        rows = connection.execute(
            """
            SELECT url_hash, title, source, url, category, summary, published_at
            FROM articles
            WHERE ready_to_send = 1
            """
            + where_category
            + " ORDER BY id DESC",
            parameters,
        ).fetchall()

    now = datetime.now(timezone.utc)
    cutoff = (
        since.astimezone(timezone.utc)
        if since is not None
        else now - timedelta(hours=hours)
    )
    upper_bound = until.astimezone(timezone.utc) if until is not None else now
    recent: list[dict] = []
    for row in rows:
        published_at = parse_published_at(row[6])
        if (
            published_at is None
            or published_at <= cutoff
            or published_at > upper_bound
        ):
            continue
        recent.append(
            {
                "url_hash": row[0],
                "title": row[1],
                "source": row[2],
                "link": row[3] or "",
                "category": row[4],
                "summary": row[5],
                "published_at": row[6],
            }
        )
    recent.sort(
        key=lambda article: parse_published_at(article["published_at"])
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return recent[:limit]


# ---------------------------------------------------------------------------
# Gemini analysis and persistence
# ---------------------------------------------------------------------------

def validate_article_analysis(result: object) -> dict:
    """Validate one classification result returned by Gemini."""
    categories = {"Tài chính", "Thị trường", "Công nghệ"}
    category = result.get("category") if isinstance(result, dict) else None
    bullet_points = result.get("bullet_points") if isinstance(result, dict) else None
    if category not in categories or not isinstance(bullet_points, list):
        raise ValueError("Gemini returned an invalid article analysis schema")
    bullet_points = [str(point).strip() for point in bullet_points]
    if not 2 <= len(bullet_points) <= 3 or any(not point for point in bullet_points):
        raise ValueError("Gemini must return 2 or 3 non-empty bullet points")
    return {"category": category, "bullet_points": bullet_points}


def article_evidence_text(entry: dict) -> str:
    """Combine RSS summary/content fields without repeating identical text."""
    parts: list[str] = []
    for value in (entry.get("summary"), entry.get("description")):
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    content = entry.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = str(item.get("value") or "").strip()
                if text and text not in parts:
                    parts.append(text)
    return "\n\n".join(parts)


def analyze_articles_batch(entries: list[dict]) -> dict[str, dict]:
    """Classify several articles in one Gemini request to reduce quota usage."""
    if gemini_client is None or genai_types is None:
        raise RuntimeError("GEMINI_API_KEY is missing")

    records = []
    ids_to_hashes: dict[str, str] = {}
    for index, entry in enumerate(entries, 1):
        item_id = f"a{index}"
        ids_to_hashes[item_id] = entry_url_hash(entry)
        notes = []
        if entry.get("_job_source"):
            role_groups = entry.get("_role_groups", list(JOB_ROLE_SKILLS))
            notes.append(
                "Dữ liệu tuyển dụng; nêu nhóm vị trí và kỹ năng chung nếu có bằng chứng, "
                f"không suy ra số lượng nhu cầu. Nhóm theo dõi: {', '.join(role_groups)}."
            )
        if entry.get("_official_source"):
            notes.append(
                "Nguồn công bố chính thống; giữ nguyên tên chỉ số, giá trị, đơn vị, "
                "kỳ so sánh và ngày công bố. Không tự suy diễn nguyên nhân."
            )
        records.append(
            {
                "id": item_id,
                "title": (entry.get("title") or "").strip(),
                "source": (entry.get("_source") or "").strip(),
                "excerpt": article_evidence_text(entry)[:GEMINI_EXCERPT_CHARS],
                "notes": " ".join(notes),
            }
        )

    prompt = f"""
Tóm tắt đầy đủ và trung thành các bài viết trong danh sách dưới đây. Phân loại
chỉ là trường phụ; mục tiêu chính là không bỏ sót thông tin quan trọng.

Chỉ trả về một JSON array thuần, không Markdown, không code fence. Phải trả về
một phần tử cho mỗi id đầu vào và giữ nguyên id:
[
  {{
    "id": "a1",
    "category": "Tài chính|Thị trường|Công nghệ",
    "bullet_points": ["...", "..."]
  }}
]

Quy tắc:
- category phải chính xác là một trong ba giá trị: Tài chính, Thị trường, Công nghệ.
- bullet_points phải có 2 hoặc 3 chuỗi; ưu tiên 3 chuỗi nếu bài có nhiều thông tin.
- Mỗi gạch đầu dòng có thể gồm 1–2 câu và phải giữ các dữ kiện quan trọng: chuyện gì xảy ra,
  chủ thể liên quan, thời điểm/địa điểm, số liệu, thay đổi, trạng thái và mốc tiếp theo nếu có.
- Giữ nguyên số liệu định lượng, đơn vị, phần trăm, tên riêng, ngày tháng và chiều hướng tăng/giảm.
- Không rút gọn đến mức mất điều kiện, phạm vi, ngoại lệ hoặc kết quả quan trọng đã nêu trong trích đoạn.
- Chỉ nêu nguyên nhân, tác động hoặc nhận định nếu trích đoạn nói rõ; không tự suy diễn và không đưa khuyến nghị đầu tư.
- Nếu trích đoạn không đủ thông tin, ghi rõ phần đó chưa được nguồn cung cấp thay vì đoán.
- Không tự bịa số liệu hoặc thông tin không có trong đầu vào.
- Chỉ dùng title, source, excerpt và notes của từng bài tương ứng.

Dữ liệu đầu vào:
{json.dumps(records, ensure_ascii=False)}
""".strip()
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    raw_text = (response.text or "").strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`").removeprefix("json").strip()
    result = json.loads(raw_text)
    if isinstance(result, dict):
        result = result.get("articles")
    if not isinstance(result, list):
        raise ValueError("Gemini batch response must be a JSON array")

    analyses: dict[str, dict] = {}
    for item in result:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", ""))
        article_hash = ids_to_hashes.get(item_id)
        if not article_hash:
            logger.warning("Ignoring Gemini result with unknown article id: %s", item_id)
            continue
        try:
            analyses[article_hash] = validate_article_analysis(item)
        except ValueError:
            logger.exception("Invalid Gemini analysis for article id %s", item_id)
    if not analyses:
        raise ValueError("Gemini returned no valid article analyses")
    if len(analyses) < len(entries):
        logger.warning(
            "Gemini analyzed %s/%s articles in one batch",
            len(analyses),
            len(entries),
        )
    return analyses


def save_article_analyses(analyses: dict[str, dict]) -> None:
    """Persist all valid results from one Gemini batch in one transaction."""
    if not analyses:
        return
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.executemany(
            """
            UPDATE articles
            SET category = ?, summary = ?, ready_to_send = 1
            WHERE url_hash = ?
            """,
            [
                (
                    analysis["category"],
                    json.dumps(analysis["bullet_points"], ensure_ascii=False),
                    article_hash,
                )
                for article_hash, analysis in analyses.items()
            ],
        )
        connection.commit()


def analyze_and_store_entries(entries: list[dict]) -> bool:
    """Analyze pending articles in quota-friendly batches."""
    # Skip articles that already have a valid result. This avoids wasting
    # Gemini quota when the same feed item appears in multiple fetches.
    article_hashes = [entry_url_hash(entry) for entry in entries]
    stored = get_article_analysis(article_hashes)
    pending: list[dict] = []
    seen_hashes: set[str] = set()
    for entry in entries:
        article_hash = entry_url_hash(entry)
        current = stored.get(article_hash)
        if (
            not article_hash
            or article_hash in seen_hashes
            or (current and current["ready_to_send"])
        ):
            continue
        seen_hashes.add(article_hash)
        pending.append(entry)

    analysis_complete = True
    for start in range(0, len(pending), GEMINI_BATCH_SIZE):
        batch = pending[start : start + GEMINI_BATCH_SIZE]
        try:
            analyses = analyze_articles_batch(batch)
            save_article_analyses(analyses)
        except Exception:
            logger.exception(
                "Gemini batch analysis failed for articles %s-%s; stopping this run",
                start + 1,
                start + len(batch),
            )
            # A failed batch must keep the checkpoint unchanged so the next
            # command can retry these articles instead of losing them.
            analysis_complete = False
            break
    return analysis_complete


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def format_entry(entry: dict) -> str:
    """Render one stored article as a Telegram-safe HTML message."""
    title = escape(entry.get("title", "Không có tiêu đề"))
    link = escape(entry.get("link", ""))
    source = escape(entry.get("source", "Nguồn không xác định") or "Nguồn không xác định")
    published_at = escape(entry.get("published_at", "") or "")
    raw_summary = entry.get("summary", "") or ""
    try:
        parsed_summary = json.loads(raw_summary)
    except (TypeError, json.JSONDecodeError):
        parsed_summary = None
    if isinstance(parsed_summary, list):
        summary = "\n".join(f"• {escape(str(point))}" for point in parsed_summary)
    else:
        summary = escape(str(raw_summary).replace("\n", " ").strip())
        if len(summary) > 400:
            summary = summary[:397] + "..."

    metadata = f"<i>{source}"
    if published_at:
        metadata += f" · {published_at}"
    metadata += "</i>"
    message = f"<b>{title}</b>\n{metadata}\n"
    if summary:
        message += f"\n{summary}\n"
    if not link:
        return message.rstrip()
    return f"{message}\n<a href=\"{link}\">Đọc bài gốc đầy đủ</a>"


# ---------------------------------------------------------------------------
# Source loading and reliability filtering
# ---------------------------------------------------------------------------

def load_sources() -> list[dict]:
    """Load enabled sources and reject invalid reliability metadata."""
    with SOURCES_PATH.open("r", encoding="utf-8") as file:
        configured_sources = json.load(file)
    if not isinstance(configured_sources, list):
        raise ValueError("sources.json must contain a list of sources")

    valid_sources: list[dict] = []
    for source in configured_sources:
        if not source.get("enabled", True):
            continue
        url = source.get("url", "")
        domain = source.get("domain", "").lower().strip()
        score = source.get("reliability_score")
        hostname = (urlparse(url).hostname or "").lower()

        # Unknown or malformed sources are rejected instead of receiving
        # an optimistic default score.
        if not url or not domain or not isinstance(score, int) or isinstance(score, bool):
            logger.warning("Skipping source with incomplete reliability metadata: %s", source)
            continue
        if not 1 <= score <= 10:
            logger.warning("Skipping source with invalid reliability score: %s", source.get("name"))
            continue
        if hostname != domain and not hostname.endswith(f".{domain}"):
            logger.warning(
                "Skipping source whose domain does not match its URL: %s",
                source.get("name"),
            )
            continue
        valid_sources.append(source)
    return valid_sources


def load_source_catalog() -> dict:
    """Load the approved-domain registry and discovery queries fail-closed."""
    if not SOURCE_CATALOG_PATH.exists():
        logger.warning("Source catalog not found: %s", SOURCE_CATALOG_PATH)
        return {"approved_domains": [], "discovery_queries": []}
    with SOURCE_CATALOG_PATH.open("r", encoding="utf-8") as file:
        catalog = json.load(file)
    if not isinstance(catalog, dict):
        raise ValueError("source_catalog.json must contain an object")
    if not isinstance(catalog.get("approved_domains", []), list):
        raise ValueError("source_catalog.json approved_domains must be a list")
    if not isinstance(catalog.get("discovery_queries", []), list):
        raise ValueError("source_catalog.json discovery_queries must be a list")
    return catalog


def approved_domain_scores() -> dict[str, int]:
    """Combine source endpoints with explicitly approved discovery domains."""
    scores: dict[str, int] = {}
    for source in load_sources():
        domain = source["domain"].lower().strip()
        scores[domain] = max(scores.get(domain, 0), source["reliability_score"])

    catalog = load_source_catalog()
    for item in catalog.get("approved_domains", []):
        if not item.get("enabled", True):
            continue
        domain = str(item.get("domain", "")).lower().strip()
        score = item.get("reliability_score")
        if domain and isinstance(score, int) and not isinstance(score, bool) and 1 <= score <= 10:
            scores[domain] = max(scores.get(domain, 0), score)
    return scores


def matching_approved_domain(url: str, scores: dict[str, int]) -> str | None:
    """Return the most specific approved domain that matches a URL."""
    hostname = (urlparse(url).hostname or "").lower().strip(".")
    if not hostname:
        return None
    matches = [
        domain
        for domain in scores
        if hostname == domain or hostname.endswith(f".{domain}")
    ]
    return max(matches, key=len) if matches else None


def fetch_discovery_entries(limit: int) -> list[dict]:
    """Discover articles from configured search feeds, accepting only approved domains."""
    catalog = load_source_catalog()
    scores = approved_domain_scores()
    if not scores:
        return []

    entries: list[dict] = []
    candidates: list[dict] = []
    seen_hashes: set[str] = set()
    for spec in catalog.get("discovery_queries", []):
        if not spec.get("enabled", True) or not spec.get("query"):
            continue
        if len(entries) >= limit:
            break
        query = str(spec["query"])
        template = spec.get(
            "url_template",
            "https://news.google.com/rss/search?q={query}&hl=vi&gl=VN&ceid=VN:vi",
        )
        feed_url = template.format(query=quote_plus(query))
        try:
            response = requests.get(
                feed_url,
                timeout=15,
                headers={"User-Agent": "TelegramNewsBot/1.0 (+controlled-discovery)"},
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except Exception:
            logger.exception("Cannot fetch discovery query: %s", query)
            continue

        per_query_limit = min(int(spec.get("max_entries", 5)), 10)
        for raw_entry in feed.entries[:per_query_limit]:
            source_data = raw_entry.get("source", {})
            source_href = source_data.get("href", "") if hasattr(source_data, "get") else ""
            source_title = source_data.get("title", "") if hasattr(source_data, "get") else ""
            article_url = str(raw_entry.get("link", "")).strip()
            discovered_domain = matching_approved_domain(source_href, scores)
            if not discovered_domain:
                candidate_url = source_href or article_url
                candidate_domain = (urlparse(candidate_url).hostname or "").lower()
                if candidate_domain and candidate_domain not in {"news.google.com", "google.com"}:
                    candidates.append(
                        {
                            "domain": candidate_domain,
                            "sample_url": article_url or candidate_url,
                            "source_name": source_title or query,
                        }
                    )
                continue
            if scores[discovered_domain] < MIN_RELIABILITY_SCORE or not article_url:
                continue
            article_hash = hashlib.sha256(article_url.encode()).hexdigest()
            if article_hash in seen_hashes:
                continue
            seen_hashes.add(article_hash)
            entry = dict(raw_entry)
            entry["_source"] = source_title or discovered_domain
            entry["_category"] = spec.get("category", "general")
            entry["_reliability_score"] = scores[discovered_domain]
            entry["_discovered_domain"] = discovered_domain
            entry["_discovery_query"] = query
            entry["_discovery_source"] = spec.get("name", "Google News RSS")
            published_struct = raw_entry.get("published_parsed") or raw_entry.get("updated_parsed")
            if published_struct:
                entry["_published_at"] = datetime(
                    *published_struct[:6], tzinfo=timezone.utc
                ).isoformat()
            else:
                entry["_published_at"] = (
                    raw_entry.get("published")
                    or raw_entry.get("updated")
                    or datetime.now(timezone.utc).isoformat()
                )
            entries.append(entry)
            if len(entries) >= limit:
                break

    record_discovery_candidates(candidates)
    return entries


def fetch_job_source_entries(source: dict) -> list[dict]:
    """Create a dated snapshot from a configured job-board page."""
    response = requests.get(
        source["url"],
        timeout=20,
        headers={"User-Agent": "TelegramNewsBot/1.0 (+job-market-monitor)"},
    )
    response.raise_for_status()
    visible_text = extract_visible_text(response.content)
    if len(visible_text) < 200:
        raise ValueError(f"Job page returned too little readable text: {source['url']}")

    local_now = datetime.now(DATABASE_TIMEZONE)
    period = "morning" if local_now.hour < 12 else "evening"
    snapshot = f"{local_now:%Y%m%d}-{period}"
    role_groups = source.get("role_groups", list(JOB_ROLE_SKILLS))
    summary = (
        f"Snapshot tuyển dụng ngày {snapshot}. "
        f"Nhóm vị trí theo dõi: {', '.join(role_groups)}. "
        f"Nội dung trang: {visible_text[:10000]}"
    )
    return [
        {
            "title": f"{source['name']} - xu hướng tuyển dụng Data/AI ({snapshot})",
            "link": f"{source['url']}#snapshot-{snapshot}",
            "summary": summary,
            "_source": source["name"],
            "_category": source.get("category", "technology"),
            "_reliability_score": source["reliability_score"],
            "_published_at": datetime.now(timezone.utc).isoformat(),
            "_job_source": True,
            "_role_groups": role_groups,
        }
    ]


def fetch_official_html_entries(source: dict) -> list[dict]:
    """Capture a dated snapshot from an official domestic data/disclosure page."""
    response = requests.get(
        source["url"],
        timeout=25,
        headers={"User-Agent": "TelegramNewsBot/1.0 (+official-data-monitor)"},
    )
    response.raise_for_status()
    visible_text = extract_visible_text(response.content)
    if len(visible_text) < 200:
        raise ValueError(f"Official page returned too little readable text: {source['url']}")

    local_now = datetime.now(DATABASE_TIMEZONE)
    period = "morning" if local_now.hour < 12 else "evening"
    snapshot = f"{local_now:%Y%m%d}-{period}"
    focus = source.get("focus", "công bố và chỉ số kinh tế, tài chính")
    summary = (
        f"Snapshot nguồn chính thống {snapshot}; phạm vi theo dõi: {focus}. "
        f"Nội dung trang: {visible_text[:12000]}"
    )
    return [
        {
            "title": f"{source['name']} - cập nhật {focus} ({snapshot})",
            "link": f"{source['url']}#snapshot-{snapshot}",
            "summary": summary,
            "_source": source["name"],
            "_category": source.get("category", "markets"),
            "_reliability_score": source["reliability_score"],
            "_published_at": datetime.now(timezone.utc).isoformat(),
            "_official_source": True,
        }
    ]


def fetch_source_entries(source: dict, limit: int) -> list[dict]:
    """Fetch one source as RSS, a job-board snapshot, or an official snapshot."""
    if source.get("format") == "html_job_page":
        return fetch_job_source_entries(source)
    if source.get("format") == "html_official_page":
        return fetch_official_html_entries(source)
    response = requests.get(
        source["url"],
        timeout=15,
        headers={"User-Agent": "TelegramNewsBot/1.0"},
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    feed_source = feed.feed.get("title", source["name"])
    entries: list[dict] = []
    for raw_entry in feed.entries[:limit]:
        entry = dict(raw_entry)
        source_data = raw_entry.get("source", {})
        source_name = source_data.get("title") if hasattr(source_data, "get") else None
        tags = raw_entry.get("tags", [])
        categories = [tag.get("term", "") for tag in tags if tag.get("term")]
        entry["_source"] = source_name or source.get("name") or feed_source
        entry["_category"] = source.get("category") or ", ".join(categories)
        entry["_reliability_score"] = source["reliability_score"]
        published_struct = raw_entry.get("published_parsed") or raw_entry.get("updated_parsed")
        if published_struct:
            entry["_published_at"] = datetime(
                *published_struct[:6], tzinfo=timezone.utc
            ).isoformat()
        else:
            entry["_published_at"] = (
                raw_entry.get("published")
                or raw_entry.get("updated")
                or datetime.now(timezone.utc).isoformat()
            )
        entries.append(entry)
    return entries


def filter_reliable_entries(entries: list[dict], source_name: str) -> list[dict]:
    """Keep only articles meeting the configured reliability threshold."""
    reliable_entries = [
        entry
        for entry in entries
        if entry.get("_reliability_score", 0) >= MIN_RELIABILITY_SCORE
    ]
    filtered_count = len(entries) - len(reliable_entries)
    if filtered_count:
        logger.info(
            "Filtered %s article(s) from %s below reliability threshold %s",
            filtered_count,
            source_name,
            MIN_RELIABILITY_SCORE,
        )
    return reliable_entries


def fetch_entries(limit: int = 5) -> list[dict]:
    """Read RSS and configured HTML snapshots from enabled sources."""
    source_entries: list[list[dict]] = []
    for source in load_sources():
        try:
            entries = fetch_source_entries(source, limit)
            source_entries.append(filter_reliable_entries(entries, source["name"]))
        except Exception:
            logger.exception("Cannot fetch source: %s", source.get("name", source.get("url")))

    discovery_entries: list[dict] = []
    if DISCOVERY_ENABLED:
        discovery_limit = min(DISCOVERY_MAX_ITEMS, max(1, limit // 3))
        try:
            discovery_entries = filter_reliable_entries(
                fetch_discovery_entries(discovery_limit),
                "controlled discovery",
            )
        except Exception:
            logger.exception("Controlled discovery failed")

    # Round-robin keeps one source from filling the entire result set.
    entries: list[dict] = []
    normal_limit = max(0, limit - len(discovery_entries))
    while len(entries) < normal_limit and any(source_entries):
        for items in source_entries:
            if items and len(entries) < normal_limit:
                entries.append(items.pop(0))
    entries.extend(discovery_entries[: limit - len(entries)])
    return entries


# ---------------------------------------------------------------------------
# On-demand news pipeline
# ---------------------------------------------------------------------------

def parse_requested_window(args: list[str]) -> timedelta | None:
    """Parse a compact, unaccented window such as 24h, 2ngay, or 1tuan."""
    if not args:
        return None
    if len(args) != 1:
        raise ValueError(
            "Sai cú pháp. Dùng: /news 24h, /news 2ngay, "
            "/news 3ngay, /news 1tuan hoặc /news 1thang."
        )

    raw = args[0].strip().lower()
    match = re.fullmatch(r"(\d+)(h|ngay|tuan|thang)", raw)
    if not match:
        raise ValueError(
            "Sai cú pháp. Khoảng thời gian phải viết liền, không dấu: "
            "24h, 2ngay, 3ngay, 5ngay, 1tuan, 2tuan hoặc 1thang."
        )

    amount = int(match.group(1))
    unit = match.group(2)
    unit_hours = {
        "h": 1,
        "ngay": 24,
        "tuan": 24 * 7,
        "thang": 24 * 30,
    }
    if unit not in unit_hours or amount <= 0:
        raise ValueError(
            "Sai cú pháp. Dùng: /news 24h, /news 2ngay, /news 3ngay, "
            "/news 5ngay, /news 1tuan, /news 2tuan hoặc /news 1thang."
        )
    return timedelta(hours=amount * unit_hours[unit])


def resolve_on_demand_window(
    chat_id: int,
    scope: str,
    args: list[str],
) -> tuple[datetime, datetime]:
    """Use an explicit window or the last successful check for this command."""
    now = datetime.now(timezone.utc)
    requested = parse_requested_window(args)
    if requested is not None:
        return now - requested, now

    checkpoint = get_chat_checkpoint(chat_id, scope)
    return checkpoint or (now - timedelta(hours=24)), now


async def collect_on_demand_news(
    category: str | None,
    since: datetime,
    until: datetime,
) -> tuple[list[dict], bool]:
    """Fetch, deduplicate, analyze, and return articles in an absolute window."""
    entries = await asyncio.to_thread(fetch_entries, 20)
    await asyncio.to_thread(save_entries, entries)
    analysis_complete = await asyncio.to_thread(analyze_and_store_entries, entries)
    articles = await asyncio.to_thread(
        get_recent_ready_articles,
        category,
        24,
        20,
        since,
        until,
    )
    links_by_hash = {entry_url_hash(entry): entry.get("link", "") for entry in entries}
    for article in articles:
        article["link"] = links_by_hash.get(article["url_hash"], "")
    return articles, analysis_complete


async def send_on_demand_news(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    scope: str,
    category: str | None = None,
) -> None:
    """Fetch, analyze, and send the requested time window to one chat."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return
    try:
        since, until = resolve_on_demand_window(chat.id, scope, context.args)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        articles, analysis_complete = await collect_on_demand_news(category, since, until)
    except Exception:
        logger.exception("On-demand news fetch failed for scope %s", scope)
        await update.message.reply_text("Không thể lấy tin lúc này. Bạn thử lại sau nhé.")
        return

    if not analysis_complete:
        await update.message.reply_text(
            "Gemini đang tạm hết hạn ngạch. Bạn chờ khoảng một phút rồi thử lại nhé."
        )
        return

    if analysis_complete:
        await asyncio.to_thread(save_chat_checkpoint, chat.id, scope, until)
    if not articles:
        await update.message.reply_text("Chưa có bài viết mới trong khoảng thời gian này.")
        return

    article_hashes = [article["url_hash"] for article in articles]
    await update.message.reply_text(
        "\n\n".join(format_entry(article) for article in articles),
        parse_mode="HTML",
    )
    await asyncio.to_thread(mark_entries_sent, article_hashes)


async def news_on_demand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the general news command."""
    await send_on_demand_news(update, context, "news")


async def tech_news_on_demand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the technology news command."""
    await send_on_demand_news(update, context, "tech", "Công nghệ")


async def finance_news_on_demand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the finance news command."""
    await send_on_demand_news(update, context, "finance", "Tài chính")


# ---------------------------------------------------------------------------
# Telegram handlers and application startup
# ---------------------------------------------------------------------------

def ask_gemini(prompt: str) -> str:
    """Send a free-form user question to Gemini and return its text reply."""
    if gemini_client is None:
        return (
            "Mình chưa được cấu hình Gemini. Để bật trả lời AI, hãy thêm "
            "GEMINI_API_KEY vào file .env rồi khởi động lại bot."
        )
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    return response.text or "Mình chưa có câu trả lời phù hợp."


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle non-command messages as free-form Gemini questions."""
    del context
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        answer = await asyncio.to_thread(ask_gemini, update.message.text)
    except Exception:
        logger.exception("Gemini request failed")
        answer = "Mình gặp lỗi khi xử lý câu hỏi. Bạn thử lại sau nhé."
    await update.message.reply_text(answer)


async def start_on_demand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain the available local, on-demand commands."""
    del context
    await update.message.reply_text(
        "Bot đang chạy ở chế độ local theo yêu cầu.\n\n"
        "/news - tin mới kể từ lần gọi /news trước\n"
        "/tech - tin Công nghệ kể từ lần gọi /tech trước\n"
        "/finance - tin Tài chính kể từ lần gọi /finance trước\n\n"
        "Khoảng thời gian phải viết liền, không dấu: /news 24h, "
        "/news 2ngay, /news 1tuan hoặc /news 1thang.\n"
        "Lần đầu không truyền khoảng thời gian sẽ lấy 24 giờ gần nhất."
    )


async def help_on_demand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias for the start message so both commands stay consistent."""
    await start_on_demand(update, context)


def build_application() -> Application:
    """Create the Telegram application and register all local handlers."""
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Add it to .env.")
    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )
    application.add_handler(CommandHandler("start", start_on_demand))
    application.add_handler(CommandHandler("help", help_on_demand))
    application.add_handler(CommandHandler("news", news_on_demand))
    application.add_handler(CommandHandler("tech", tech_news_on_demand))
    application.add_handler(CommandHandler("finance", finance_news_on_demand))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    return application


def main() -> None:
    """Start long polling; the process must remain open for the bot to work."""
    logger.info("Starting Telegram bot")
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
