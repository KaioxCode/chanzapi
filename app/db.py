from __future__ import annotations

import json
import queue
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import requests

from .config import settings

RAW_DB_PATH = settings.database_url.replace("sqlite:///", "")
DB_PATH = Path(RAW_DB_PATH)
if not DB_PATH.is_absolute():
    DB_PATH = (Path(__file__).resolve().parents[1] / DB_PATH).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

QUERY_JSON_DIR = Path(settings.query_json_dir)
if not QUERY_JSON_DIR.is_absolute():
    QUERY_JSON_DIR = (Path(__file__).resolve().parents[1] / QUERY_JSON_DIR).resolve()
QUERY_JSON_DIR.mkdir(parents=True, exist_ok=True)

listeners: set[queue.Queue] = set()
listeners_lock = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT UNIQUE NOT NULL,
                label TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS query_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_type TEXT NOT NULL,
                query_value TEXT NOT NULL,
                response_json TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                api_key TEXT,
                ip_address TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def notify_listeners(event: Dict[str, Any]) -> None:
    dead: list[queue.Queue] = []
    with listeners_lock:
        for listener in listeners:
            try:
                listener.put_nowait(event)
            except Exception:
                dead.append(listener)

        for item in dead:
            listeners.discard(item)


def register_listener() -> queue.Queue:
    q: queue.Queue = queue.Queue()
    with listeners_lock:
        listeners.add(q)
    return q


def unregister_listener(q: queue.Queue) -> None:
    with listeners_lock:
        listeners.discard(q)


def log_admin_action(action: str, detail: Dict[str, Any]) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO admin_audit (action, detail_json, created_at) VALUES (?, ?, ?)",
            (action, json.dumps(detail, ensure_ascii=False), utc_now_iso()),
        )


def create_api_key(api_key: str, label: Optional[str] = None) -> Dict[str, Any]:
    now = utc_now_iso()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, label, created_at) VALUES (?, ?, ?)",
            (api_key, label, now),
        )

    log_admin_action("create_api_key", {"api_key": api_key, "label": label})

    return {
        "api_key": api_key,
        "label": label,
        "created_at": now,
        "is_active": True,
        "active": True,
    }


def list_api_keys() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT api_key, label, is_active, created_at, last_used_at
            FROM api_keys
            ORDER BY id DESC
            """
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["active"] = bool(item["is_active"])
        items.append(item)
    return items


def get_api_key_record(api_key: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT api_key, label, is_active, created_at, last_used_at
            FROM api_keys
            WHERE api_key = ?
            LIMIT 1
            """,
            (api_key,),
        ).fetchone()

    if not row:
        return None

    item = dict(row)
    item["active"] = bool(item["is_active"])
    return item


def api_key_exists(api_key: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT api_key FROM api_keys WHERE api_key = ?",
            (api_key,),
        ).fetchone()
    return row is not None


def validate_api_key_db(api_key: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT api_key FROM api_keys WHERE api_key = ? AND is_active = 1",
            (api_key,),
        ).fetchone()
    return row is not None


def touch_api_key(api_key: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE api_key = ?",
            (utc_now_iso(), api_key),
        )


def deactivate_api_key(api_key: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE api_key = ?",
            (api_key,),
        )
        changed = cur.rowcount > 0

    if changed:
        log_admin_action("deactivate_api_key", {"api_key": api_key})

    return changed


def get_query_json_path(query_type: str) -> Path:
    safe_name = "".join(ch for ch in query_type.lower().strip() if ch.isalnum() or ch in {"_", "-"}) or "unknown"
    return QUERY_JSON_DIR / f"{safe_name}.json"


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"query_type": path.stem, "updated_at": utc_now_iso(), "items": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"query_type": path.stem, "updated_at": utc_now_iso(), "items": []}


def upsert_query_json_record(*, query_type: str, query_value: str, response_data: Dict[str, Any], status_code: int, api_key: Optional[str], ip_address: Optional[str], created_at: Optional[str] = None) -> Dict[str, Any]:
    path = get_query_json_path(query_type)
    payload = _read_json_file(path)
    items = payload.setdefault("items", [])
    created = created_at or utc_now_iso()
    record = {
        "query_value": query_value,
        "status_code": status_code,
        "api_key": api_key,
        "ip_address": ip_address,
        "created_at": created,
        "response_data": response_data,
    }
    items.append(record)
    payload["query_type"] = query_type
    payload["updated_at"] = utc_now_iso()
    payload["total_items"] = len(items)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def list_query_json_files() -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    for path in sorted(QUERY_JSON_DIR.glob("*.json")):
        data = _read_json_file(path)
        files.append(
            {
                "query_type": data.get("query_type", path.stem),
                "filename": path.name,
                "updated_at": data.get("updated_at"),
                "total_items": len(data.get("items", [])),
                "path": str(path),
            }
        )
    return files


def get_query_json_file(query_type: str) -> Dict[str, Any]:
    return _read_json_file(get_query_json_path(query_type))


def overwrite_query_json_file(query_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    path = get_query_json_path(query_type)
    payload = {
        "query_type": query_type,
        "updated_at": utc_now_iso(),
        "items": data.get("items", []),
    }
    payload["total_items"] = len(payload["items"])
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log_admin_action("overwrite_query_json_file", {"query_type": query_type, "total_items": payload["total_items"]})
    return payload


def export_full_database_snapshot() -> Dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "api_keys": list_api_keys(),
        "logs": list_query_logs(limit=5000),
        "query_files": list_query_json_files(),
    }


def _send_to_base44(payload: Dict[str, Any]) -> None:
    if not settings.base44_webhook_url:
        return

    headers = {}
    if settings.base44_webhook_secret:
        headers["x-webhook-secret"] = settings.base44_webhook_secret

    try:
        requests.post(settings.base44_webhook_url, json=payload, headers=headers, timeout=8)
    except requests.RequestException:
        pass


def save_query_log(
    *,
    query_type: str,
    query_value: str,
    response_data: Dict[str, Any],
    status_code: int,
    api_key: Optional[str],
    ip_address: Optional[str],
) -> Dict[str, Any]:
    created_at = utc_now_iso()

    row = {
        "query_type": query_type,
        "query_value": query_value,
        "response_json": json.dumps(response_data, ensure_ascii=False),
        "status_code": status_code,
        "api_key": api_key,
        "ip_address": ip_address,
        "created_at": created_at,
    }

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO query_logs (
                query_type, query_value, response_json, status_code, api_key, ip_address, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["query_type"],
                row["query_value"],
                row["response_json"],
                row["status_code"],
                row["api_key"],
                row["ip_address"],
                row["created_at"],
            ),
        )
        row["id"] = cur.lastrowid

    upsert_query_json_record(
        query_type=query_type,
        query_value=query_value,
        response_data=response_data,
        status_code=status_code,
        api_key=api_key,
        ip_address=ip_address,
        created_at=created_at,
    )

    event_payload = {
        "event": "new_query_log",
        "log": {
            **row,
            "response_json": response_data,
        },
    }

    notify_listeners(event_payload)
    _send_to_base44(event_payload)

    if api_key:
        touch_api_key(api_key)

    return event_payload["log"]


def list_query_logs(limit: int = 100) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, query_type, query_value, response_json, status_code, api_key, ip_address, created_at
            FROM query_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        item["response_json"] = json.loads(item["response_json"])
        results.append(item)

    return results


def get_dashboard_stats() -> Dict[str, Any]:
    with get_conn() as conn:
        total_queries = conn.execute("SELECT COUNT(*) FROM query_logs").fetchone()[0]
        total_keys = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
        active_keys = conn.execute("SELECT COUNT(*) FROM api_keys WHERE is_active = 1").fetchone()[0]
        inactive_keys = conn.execute("SELECT COUNT(*) FROM api_keys WHERE is_active = 0").fetchone()[0]
        last_queries = conn.execute(
            """
            SELECT query_type, COUNT(*) AS total
            FROM query_logs
            GROUP BY query_type
            ORDER BY total DESC
            LIMIT 10
            """
        ).fetchall()

    return {
        "total_queries": total_queries,
        "total_keys": total_keys,
        "active_keys": active_keys,
        "inactive_keys": inactive_keys,
        "top_queries": [dict(row) for row in last_queries],
        "json_files": list_query_json_files(),
    }
