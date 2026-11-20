"""SuperGram global NFT registry backed by Neon PostgreSQL.

Integration in main.py:
    from nft_server_neon_fixed import router as nft_router
    app.include_router(nft_router)

Requirements:
    fastapi
    psycopg2-binary
    pydantic

Render environment:
    DATABASE_URL=<Neon connection string with sslmode=require>
    NFT_VERIFY_URL=https://supergram.onrender.com/activate

The existing activation endpoint must accept POST JSON {"key": ..., "telegram_id": ...}
and return JSON containing {"ok": true} for a valid license.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Dict, List, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["NFT"])

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
VERIFY_URL = os.environ.get(
    "NFT_VERIFY_URL", "https://supergram.onrender.com/activate"
).strip()
MAX_ITEMS_PER_PUBLISHER = 60
MAX_BLOB = 60_000
MAX_TARGETS = 40
MAX_KEY_LENGTH = 80
VERIFY_TTL = 600

_verified: Dict[Tuple[str, int], float] = {}
_verified_lock = threading.Lock()


def _db():
    """Open a short-lived database connection; fail with a useful server error."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL_NOT_CONFIGURED")
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=10, sslmode="require")
    except psycopg2.Error as exc:
        # Do not return connection strings or other secrets to the client.
        raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc


def _init_db() -> None:
    con = _db()
    try:
        with con, con.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS nft_items (
                    publisher_id BIGINT NOT NULL,
                    dialog_id BIGINT NOT NULL,
                    item_key TEXT NOT NULL,
                    gift TEXT NOT NULL,
                    from_id BIGINT NOT NULL DEFAULT 0,
                    anonymous BOOLEAN NOT NULL DEFAULT FALSE,
                    message TEXT NOT NULL DEFAULT '',
                    pin BOOLEAN NOT NULL DEFAULT FALSE,
                    title TEXT NOT NULL DEFAULT '',
                    slug TEXT NOT NULL DEFAULT '',
                    ts DOUBLE PRECISION NOT NULL DEFAULT 0,
                    PRIMARY KEY (publisher_id, dialog_id, item_key)
                )"""
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_nft_items_dialog_ts "
                "ON nft_items (dialog_id, ts DESC)"
            )
    finally:
        con.close()


@router.on_event("startup")
def _startup() -> None:
    # Avoid crashing during module import; startup logs will show configuration errors.
    _init_db()


def _license_ok(key: str, tg_id: int) -> bool:
    key = (key or "").strip().upper()
    if not key or tg_id <= 0 or not VERIFY_URL:
        return False

    now = time.time()
    cache_key = (key, tg_id)
    with _verified_lock:
        if _verified.get(cache_key, 0) > now:
            return True

    try:
        payload = json.dumps(
            {"key": key, "telegram_id": str(tg_id)}, separators=(",", ":")
        ).encode("utf-8")
        req = urllib.request.Request(
            VERIFY_URL,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status < 200 or response.status >= 300:
                return False
            data = json.loads(response.read().decode("utf-8", "replace"))
        ok = isinstance(data, dict) and data.get("ok") is True
    except Exception:
        return False

    if ok:
        with _verified_lock:
            _verified[cache_key] = now + VERIFY_TTL
            if len(_verified) > 5000:
                # Expire old entries first; clear only if cache remains oversized.
                for k, expiry in list(_verified.items()):
                    if expiry <= now:
                        _verified.pop(k, None)
                if len(_verified) > 5000:
                    _verified.clear()
    return ok


class Item(BaseModel):
    dialog_id: int
    key: str = Field(min_length=1, max_length=MAX_KEY_LENGTH)
    gift: str = Field(min_length=1, max_length=MAX_BLOB)
    from_id: int = 0
    anonymous: bool = False
    message: str = Field(default="", max_length=400)
    pin: bool = False
    title: str = Field(default="", max_length=120)
    slug: str = Field(default="", max_length=120)
    ts: float = 0


class SyncBody(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=24)
    key: str = Field(min_length=1, max_length=160)
    items: List[Item] = Field(default_factory=list)
    # Set true only when the client sends the complete current snapshot for this account.
    # Existing clients omit it, so their sync is non-destructive and uses upserts.
    replace_all: bool = False


class LookupBody(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=24)
    key: str = Field(min_length=1, max_length=160)
    targets: List[str] = Field(default_factory=list)


def _parse_telegram_id(value: str) -> int:
    try:
        uid = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="BAD_ID")
    if uid <= 0:
        raise HTTPException(status_code=400, detail="BAD_ID")
    return uid


def _require_license(key: str, uid: int) -> None:
    if not _license_ok(key, uid):
        raise HTTPException(status_code=403, detail="LICENSE")


@router.post("/nft/sync")
def nft_sync(body: SyncBody):
    uid = _parse_telegram_id(body.telegram_id)
    _require_license(body.key, uid)

    rows = []
    for item in body.items[:MAX_ITEMS_PER_PUBLISHER]:
        if item.dialog_id <= 0 or not item.gift:
            continue
        rows.append(
            (
                uid,
                item.dialog_id,
                item.key[:MAX_KEY_LENGTH],
                item.gift,
                max(0, int(item.from_id)),
                bool(item.anonymous),
                item.message[:400],
                bool(item.pin),
                item.title[:120],
                item.slug[:120],
                float(item.ts or time.time()),
            )
        )

    con = _db()
    try:
        with con, con.cursor() as cur:
            if body.replace_all:
                # Explicit full-snapshot mode only. The deletion and inserts are one transaction.
                cur.execute("DELETE FROM nft_items WHERE publisher_id = %s", (uid,))
            if rows:
                execute_values(
                    cur,
                    """INSERT INTO nft_items
                       (publisher_id, dialog_id, item_key, gift, from_id, anonymous,
                        message, pin, title, slug, ts)
                       VALUES %s
                       ON CONFLICT (publisher_id, dialog_id, item_key) DO UPDATE SET
                         gift = EXCLUDED.gift,
                         from_id = EXCLUDED.from_id,
                         anonymous = EXCLUDED.anonymous,
                         message = EXCLUDED.message,
                         pin = EXCLUDED.pin,
                         title = EXCLUDED.title,
                         slug = EXCLUDED.slug,
                         ts = EXCLUDED.ts""",
                    rows,
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="NFT_SYNC_FAILED") from exc
    finally:
        con.close()

    return {"ok": True, "stored": len(rows), "mode": "replace" if body.replace_all else "upsert"}


@router.post("/nft/lookup")
def nft_lookup(body: LookupBody):
    uid = _parse_telegram_id(body.telegram_id)
    _require_license(body.key, uid)

    targets = []
    for value in body.targets[:MAX_TARGETS]:
        try:
            target = int(value)
            if target > 0 and target not in targets:
                targets.append(target)
        except (TypeError, ValueError):
            continue

    users = {str(target): [] for target in targets}
    if not targets:
        return {"ok": True, "users": users}

    con = _db()
    try:
        with con, con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT publisher_id, dialog_id, item_key, gift, from_id,
                          anonymous, message, pin, title, slug, ts
                   FROM nft_items
                   WHERE dialog_id = ANY(%s)
                   ORDER BY ts DESC
                   LIMIT %s""",
                (targets, len(targets) * MAX_ITEMS_PER_PUBLISHER * 20),
            )
            for row in cur.fetchall():
                users[str(row["dialog_id"])].append(
                    {
                        "publisher_id": row["publisher_id"],
                        "key": row["item_key"],
                        "gift": row["gift"],
                        "from_id": row["from_id"],
                        "anonymous": bool(row["anonymous"]),
                        "message": row["message"],
                        "pin": bool(row["pin"]),
                        "title": row["title"],
                        "slug": row["slug"],
                        "ts": row["ts"],
                    }
                )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="NFT_LOOKUP_FAILED") from exc
    finally:
        con.close()

    return {"ok": True, "users": users}
