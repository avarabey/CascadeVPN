from __future__ import annotations

import sqlite3
import time
from typing import Any

from ..config import Config
from ..database import Database
from ..outbound import FetchError, UnsafeURLError, fetch_url, validate_outbound_url


def service_row(row: sqlite3.Row, *, include_url: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "status_code": row["status_code"],
        "latency_ms": row["latency_ms"],
        "checked_at": row["checked_at"],
        "enabled": bool(row["enabled"]),
    }
    if include_url:
        result["url"] = row["url"]
        result["error"] = row["error"]
    return result


def list_services(database: Database, *, include_url: bool = True) -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM services ORDER BY name COLLATE NOCASE").fetchall()
    return [service_row(row, include_url=include_url) for row in rows]


def add_service(database: Database, config: Config, name: str, url: str) -> dict[str, Any]:
    name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= 80:
        raise ValueError("name must contain between 1 and 80 characters")
    validated = validate_outbound_url(
        url,
        allow_private=config.allow_private_urls,
        allowed_ports=config.allowed_outbound_ports,
    )
    now = int(time.time())
    try:
        with database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO services(name, url, created_at) VALUES (?, ?, ?)",
                (name, validated.url, now),
            )
            row = connection.execute(
                "SELECT * FROM services WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ValueError("this service URL already exists") from exc
    return service_row(row)


def delete_service(database: Database, service_id: int) -> bool:
    with database.connect() as connection:
        cursor = connection.execute("DELETE FROM services WHERE id = ?", (service_id,))
    return cursor.rowcount > 0


def check_service(database: Database, config: Config, service_id: int) -> dict[str, Any]:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM services WHERE id = ?", (service_id,)
        ).fetchone()
    if row is None:
        raise KeyError(service_id)

    started = time.monotonic()
    checked_at = int(time.time())
    status_code: int | None = None
    error: str | None = None
    try:
        result = fetch_url(
            row["url"],
            allow_private=config.allow_private_urls,
            allowed_ports=config.allowed_outbound_ports,
            timeout=config.check_timeout,
            max_bytes=0,
            max_redirects=3,
        )
        status_code = result.status
        if 200 <= result.status < 400:
            status = "online"
        elif 400 <= result.status < 500:
            status = "degraded"
        else:
            status = "down"
    except (FetchError, UnsafeURLError) as exc:
        status = "down"
        error = str(exc)[:240]
    latency_ms = max(0, round((time.monotonic() - started) * 1000))

    with database.connect() as connection:
        connection.execute(
            "UPDATE services SET status = ?, status_code = ?, latency_ms = ?, "
            "checked_at = ?, error = ? WHERE id = ?",
            (status, status_code, latency_ms, checked_at, error, service_id),
        )
        updated = connection.execute(
            "SELECT * FROM services WHERE id = ?", (service_id,)
        ).fetchone()
    return service_row(updated)


def check_all(database: Database, config: Config) -> list[dict[str, Any]]:
    with database.connect() as connection:
        ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM services WHERE enabled = 1 ORDER BY id"
            )
        ]
    return [check_service(database, config, service_id) for service_id in ids]


def status_summary(services: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"total": len(services), "online": 0, "degraded": 0, "down": 0, "unknown": 0}
    for service in services:
        key = service["status"] if service["status"] in summary else "unknown"
        summary[key] += 1
    return summary
