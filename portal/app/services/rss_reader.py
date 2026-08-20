from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from ..config import Config
from ..database import Database
from ..outbound import FetchError, UnsafeURLError, fetch_url, validate_outbound_url, validate_redirect_target


MAX_FEED_BYTES = 2 * 1024 * 1024


class FeedParseError(ValueError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: str | None, limit: int) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value or "")
    except (ValueError, AssertionError):
        return ""
    result = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return result[:limit]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _child_text(element: ET.Element, *names: str) -> str:
    for child in element:
        if _local_name(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def _normalize_date(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value[:80]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _item_link(item: ET.Element) -> str | None:
    for link in _children(item, "link"):
        candidate = link.attrib.get("href") or (link.text or "").strip()
        rel = link.attrib.get("rel", "alternate")
        if candidate and rel in {"alternate", ""}:
            try:
                return validate_redirect_target(candidate)
            except ValueError:
                continue
    return None


def parse_feed(payload: bytes) -> list[dict[str, str | None]]:
    upper_payload = payload.upper()
    if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
        raise FeedParseError("DTD and XML entities are not allowed")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FeedParseError("feed is not valid XML") from exc

    root_name = _local_name(root.tag)
    if root_name == "rss":
        channel = next(iter(_children(root, "channel")), root)
        elements = _children(channel, "item")
    elif root_name in {"feed", "rdf"}:
        elements = _children(root, "entry") or _children(root, "item")
    else:
        raise FeedParseError("document is not an RSS or Atom feed")

    items: list[dict[str, str | None]] = []
    for element in elements[:200]:
        title = _plain_text(_child_text(element, "title"), 300) or "Untitled"
        link = _item_link(element)
        published = _normalize_date(
            _child_text(element, "pubdate", "published", "updated", "date")
        )
        description = _child_text(element, "description", "summary", "content", "encoded")
        excerpt = _plain_text(description, 600)
        raw_guid = _child_text(element, "guid", "id") or link or f"{title}\n{published or ''}"
        guid = hashlib.sha256(raw_guid.encode("utf-8", "replace")).hexdigest()
        items.append(
            {
                "guid": guid,
                "title": title,
                "url": link,
                "published_at": published,
                "excerpt": excerpt,
            }
        )
    return items


def feed_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "url": row["url"],
        "enabled": bool(row["enabled"]),
        "checked_at": row["checked_at"],
        "error": row["error"],
        "created_at": row["created_at"],
    }


def list_feeds(database: Database) -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM feeds ORDER BY title COLLATE NOCASE").fetchall()
    return [feed_row(row) for row in rows]


def add_feed(database: Database, config: Config, title: str, url: str) -> dict[str, Any]:
    title = title.strip() if isinstance(title, str) else ""
    validated = validate_outbound_url(
        url,
        allow_private=config.allow_private_urls,
        allowed_ports=config.allowed_outbound_ports,
    )
    if not title:
        title = urlsplit(validated.url).hostname or "RSS"
    if not 1 <= len(title) <= 100:
        raise ValueError("title must contain between 1 and 100 characters")
    now = int(time.time())
    try:
        with database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO feeds(title, url, created_at) VALUES (?, ?, ?)",
                (title, validated.url, now),
            )
            row = connection.execute(
                "SELECT * FROM feeds WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ValueError("this feed URL already exists") from exc
    return feed_row(row)


def delete_feed(database: Database, feed_id: int) -> bool:
    with database.connect() as connection:
        cursor = connection.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    return cursor.rowcount > 0


def refresh_feed(database: Database, config: Config, feed_id: int) -> dict[str, Any]:
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    if row is None:
        raise KeyError(feed_id)
    checked_at = int(time.time())
    try:
        result = fetch_url(
            row["url"],
            allow_private=config.allow_private_urls,
            allowed_ports=config.allowed_outbound_ports,
            timeout=config.feed_timeout,
            max_bytes=MAX_FEED_BYTES,
            accept="application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9",
            max_redirects=3,
        )
        if not 200 <= result.status < 300:
            raise FetchError(f"feed returned HTTP {result.status}")
        items = parse_feed(result.body)
        with database.connect() as connection:
            for item in items:
                connection.execute(
                    "INSERT INTO feed_items(feed_id, guid, title, url, published_at, excerpt, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(feed_id, guid) DO UPDATE SET title=excluded.title, "
                    "url=excluded.url, published_at=excluded.published_at, excerpt=excluded.excerpt, "
                    "fetched_at=excluded.fetched_at",
                    (
                        feed_id,
                        item["guid"],
                        item["title"],
                        item["url"],
                        item["published_at"],
                        item["excerpt"],
                        checked_at,
                    ),
                )
            connection.execute(
                "DELETE FROM feed_items WHERE feed_id = ? AND id NOT IN "
                "(SELECT id FROM feed_items WHERE feed_id = ? ORDER BY fetched_at DESC, id DESC LIMIT 1000)",
                (feed_id, feed_id),
            )
            connection.execute(
                "UPDATE feeds SET checked_at = ?, error = NULL WHERE id = ?",
                (checked_at, feed_id),
            )
    except (FetchError, UnsafeURLError, FeedParseError) as exc:
        with database.connect() as connection:
            connection.execute(
                "UPDATE feeds SET checked_at = ?, error = ? WHERE id = ?",
                (checked_at, str(exc)[:240], feed_id),
            )
        raise
    return {"feed_id": feed_id, "items_updated": len(items), "checked_at": checked_at}


def refresh_all(database: Database, config: Config) -> list[dict[str, Any]]:
    with database.connect() as connection:
        ids = [row["id"] for row in connection.execute("SELECT id FROM feeds WHERE enabled = 1")]
    results: list[dict[str, Any]] = []
    for feed_id in ids:
        try:
            results.append(refresh_feed(database, config, feed_id))
        except (FetchError, UnsafeURLError, FeedParseError) as exc:
            results.append({"feed_id": feed_id, "error": str(exc)})
    return results


def list_items(database: Database, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT i.id, i.title, i.url, i.published_at, i.excerpt, i.fetched_at, "
            "f.id AS source_id, f.title AS source FROM feed_items i "
            "JOIN feeds f ON f.id = i.feed_id "
            "ORDER BY COALESCE(i.published_at, '') DESC, i.fetched_at DESC, i.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
