"""
SUPERGRAM API — единый бэкенд (FastAPI + Neon PostgreSQL).

Лицензии, NFT-реестр, глобальный баланс, игры на виртуальные очки, статистика
и админ-эндпоинты (включая пополнение баланса пользователю).

Переменные окружения:
  DATABASE_URL   строка подключения Neon (обязательно)
  ADMIN_SECRET   длинный случайный секрет для /admin/* (обязательно для админки)
  START_COINS    стартовый баланс нового игрока (по умолчанию 100)
  REQUIRE_LICENSE  1/0 — требовать лицензионный ключ для игр/переводов/NFT (по умолчанию 1)
  DAILY_REWARD_MIN / DAILY_REWARD_MAX / DAILY_COOLDOWN_SECONDS — ежедневный бонус
"""
import hashlib
import hmac
import logging
import os
import random
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, BeforeValidator

try:  # на самом сервере драйвер всегда есть
    import psycopg2
    import psycopg2.pool
except ImportError:  # pragma: no cover
    psycopg2 = None

VERSION = "6.0.0"
log = logging.getLogger("supergram")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ───────────────────────────── настройки ─────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def _clean_dsn(raw: str) -> str:
    """Убирает типичный мусор при копировании из Neon: `psql '...'`, кавычки, пробелы."""
    s = (raw or "").strip()
    if s.lower().startswith("psql"):
        s = s[4:].strip()
    s = s.strip("'\"` \n\r\t")
    if s and "sslmode=" not in s:
        s += ("&" if "?" in s else "?") + "sslmode=require"
    return s


DATABASE_URL = _clean_dsn(os.environ.get("DATABASE_URL", ""))
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "").strip()
START_COINS = _env_int("START_COINS", 100)
REQUIRE_LICENSE = os.environ.get("REQUIRE_LICENSE", "1").strip() != "0"
DAILY_MIN = _env_int("DAILY_REWARD_MIN", 100)
DAILY_MAX = max(DAILY_MIN, _env_int("DAILY_REWARD_MAX", 300))
DAILY_COOLDOWN = _env_int("DAILY_COOLDOWN_SECONDS", 10800)  # 3 часа, как в тексте плагина

MAX_BET = 150_000_000_000          # лимит ставки в плагине
MAX_TRANSFER = 1_000_000_000       # лимит перевода в плагине
MAX_ADMIN_DELTA = 10 ** 15
MAX_COINS = 10 ** 18
MAX_NFT_ITEMS = 60                 # максимум элементов за один /nft/sync
MAX_GIFT_CHARS = 300_000
ROUND_TTL = 3600                   # раунд без действий сгорает через час
ROUND_RTP = 0.96
MAX_MULT = 1000.0
MAX_STEPS = 30

rng = random.SystemRandom()

# Мгновенные игры: (исход, вероятность, множитель выплаты)
INSTANT_GAMES = {
    "fut": [("goal", 0.55, 1.75), ("miss", 0.45, 0.0)],
    "bas": [("score", 0.35, 2.5), ("rim", 0.10, 1.0), ("loss", 0.55, 0.0)],
    "casino": [("jackpot", 0.04, 10.0), ("win", 0.30, 1.8), ("loss", 0.66, 0.0)],
}
INSTANT_ALIASES = {
    "fut": "fut", "football": "fut", "bas": "bas", "basket": "bas", "basketball": "bas",
    "casino": "casino", "casik": "casino",
}
# Раундовые игры: шанс успеха шага, названия исходов
ROUND_GAMES = {
    "mines": {"p": 0.80, "ok": "safe", "bad": "mine"},
    "tower": {"p": 0.75, "ok": "climb", "bad": "fall"},
    "roc": {"p": 0.60, "ok": "rock_win", "bad": "rock_loss"},
    "casino": {"p": 0.70, "ok": "win", "bad": "loss"},
}
RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
CHOICE_ALIASES = {"красное": "red", "красный": "red", "чёрное": "black", "черное": "black",
                  "чёрный": "black", "черный": "black", "зеро": "0"}


# ───────────────────────────── ошибки ─────────────────────────────

class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: Any = "", **extra: Any):
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def insufficient(coins: int) -> ApiError:
    return ApiError(400, "INSUFFICIENT_BALANCE", "Insufficient balance", coins=coins)


# ───────────────────────────── база данных ─────────────────────────────

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sg_licenses (
        license_key TEXT PRIMARY KEY,
        telegram_id TEXT,
        revoked BOOLEAN NOT NULL DEFAULT FALSE,
        note TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        activated_at BIGINT,
        last_check_at BIGINT)""",
    "CREATE INDEX IF NOT EXISTS idx_sg_licenses_tg ON sg_licenses (telegram_id)",
    """CREATE TABLE IF NOT EXISTS sg_users (
        telegram_id TEXT PRIMARY KEY,
        coins BIGINT NOT NULL DEFAULT 0,
        last_daily BIGINT NOT NULL DEFAULT 0,
        games_played BIGINT NOT NULL DEFAULT 0,
        wins BIGINT NOT NULL DEFAULT 0,
        total_wagered BIGINT NOT NULL DEFAULT 0,
        total_won BIGINT NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_sg_users_coins ON sg_users (coins)",
    """CREATE TABLE IF NOT EXISTS sg_game_history (
        id TEXT PRIMARY KEY,
        telegram_id TEXT NOT NULL,
        game TEXT NOT NULL,
        bet BIGINT NOT NULL,
        payout BIGINT NOT NULL,
        net BIGINT NOT NULL,
        outcome TEXT NOT NULL,
        created_at BIGINT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_sg_history_user ON sg_game_history (telegram_id, created_at)",
    """CREATE TABLE IF NOT EXISTS sg_rounds (
        telegram_id TEXT NOT NULL,
        game TEXT NOT NULL,
        bet BIGINT NOT NULL,
        steps INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL,
        PRIMARY KEY (telegram_id, game))""",
    """CREATE TABLE IF NOT EXISTS sg_coin_log (
        id TEXT PRIMARY KEY,
        telegram_id TEXT NOT NULL,
        delta BIGINT NOT NULL,
        reason TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_sg_coinlog_user ON sg_coin_log (telegram_id, created_at)",
    """CREATE TABLE IF NOT EXISTS sg_nft_items (
        publisher_id TEXT NOT NULL,
        dialog_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        gift TEXT NOT NULL,
        from_id TEXT NOT NULL DEFAULT '0',
        anonymous BOOLEAN NOT NULL DEFAULT FALSE,
        message TEXT NOT NULL DEFAULT '',
        pin BOOLEAN NOT NULL DEFAULT FALSE,
        title TEXT NOT NULL DEFAULT '',
        slug TEXT NOT NULL DEFAULT '',
        ts DOUBLE PRECISION NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL,
        PRIMARY KEY (publisher_id, dialog_id, item_key))""",
    "CREATE INDEX IF NOT EXISTS idx_sg_nft_dialog ON sg_nft_items (dialog_id)",
]

_pool = None
_pool_lock = threading.Lock()
_schema_ready = False
_schema_lock = threading.Lock()
DB_ERRORS = ((psycopg2.OperationalError, psycopg2.InterfaceError) if psycopg2 else ())


def _get_conn():
    """Соединение из пула. Пул создаётся лениво, чтобы сервер стартовал даже при недоступной БД."""
    global _pool
    if psycopg2 is None:
        raise ApiError(503, "DB_DRIVER_MISSING", "psycopg2 is not installed")
    if not DATABASE_URL:
        raise ApiError(503, "DB_NOT_CONFIGURED", "DATABASE_URL is not set")
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                1, 8, DATABASE_URL, connect_timeout=10,
                keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5)
        pool = _pool
    conn = pool.getconn()
    if conn.closed:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    return conn


def _put_conn(conn):
    try:
        if _pool is not None:
            _pool.putconn(conn)
    except Exception:
        pass


def _reset_pool():
    """Neon усыпляет вычислитель, и соединения в пуле «протухают» — выкидываем весь пул."""
    global _pool
    with _pool_lock:
        old, _pool = _pool, None
    if old is not None:
        try:
            old.closeall()
        except Exception:
            pass


def q_all(cur, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    cur.execute(sql, params)
    if cur.description is None:
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def q_one(cur, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = q_all(cur, sql, params)
    return rows[0] if rows else None


def _init_schema(cur):
    for statement in SCHEMA:
        cur.execute(statement)


def run(fn: Callable[[Any], Any]):
    """Выполняет fn(cur) в одной транзакции. При обрыве соединения повторяет до 3 раз."""
    global _schema_ready
    last: Optional[Exception] = None
    for _attempt in range(3):
        conn = None
        try:
            conn = _get_conn()
            cur = conn.cursor()
            if not _schema_ready:
                with _schema_lock:
                    if not _schema_ready:
                        _init_schema(cur)
                        conn.commit()
                        _schema_ready = True
            result = fn(cur)
            conn.commit()
            _put_conn(conn)
            return result
        except DB_ERRORS as exc:
            last = exc
            log.warning("DB connection problem, retrying: %s", exc)
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            _reset_pool()
            time.sleep(0.4)
        except BaseException:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _put_conn(conn)
            raise
    raise ApiError(503, "DB_UNAVAILABLE", "Database is unavailable: %s" % str(last)[:160])


# ───────────────────────────── утилиты ─────────────────────────────

def now() -> int:
    return int(time.time())


def new_id() -> str:
    return secrets.token_hex(10)


def check_tg(value: Any) -> str:
    s = str(value or "").strip()
    if not re.fullmatch(r"\d{1,18}", s) or int(s) <= 0:
        raise ApiError(400, "INVALID_TELEGRAM_ID", "Invalid key or Telegram ID")
    return s


def norm_key(value: Any) -> str:
    return str(value or "").strip().upper()


def check_bet(amount: Any, limit: int = MAX_BET) -> int:
    try:
        bet = int(amount)
    except (TypeError, ValueError):
        raise ApiError(400, "BAD_AMOUNT", "Amount must be an integer")
    if bet < 1 or bet > limit:
        raise ApiError(400, "BAD_AMOUNT", "Amount must be from 1 to %d" % limit)
    return bet


KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_key() -> str:
    def part(n):
        return "".join(secrets.choice(KEY_ALPHABET) for _ in range(n))
    return "SGM-%s-%s-%s" % (part(7), part(8), part(8))


def ensure_user(cur, tg: str):
    t = now()
    cur.execute(
        "INSERT INTO sg_users (telegram_id, coins, created_at, updated_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (telegram_id) DO NOTHING", (tg, START_COINS, t, t))


def get_coins(cur, tg: str) -> int:
    row = q_one(cur, "SELECT coins FROM sg_users WHERE telegram_id = %s", (tg,))
    return int(row["coins"]) if row else 0


def log_coins(cur, tg: str, delta: int, reason: str, note: str = ""):
    cur.execute(
        "INSERT INTO sg_coin_log (id, telegram_id, delta, reason, note, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (new_id(), tg, int(delta), reason, (note or "")[:200], now()))


def require_license(cur, tg_raw: Any, key_raw: Any) -> str:
    """Проверяет, что ключ существует, не отозван и привязан именно к этому Telegram ID."""
    tg = check_tg(tg_raw)
    if not REQUIRE_LICENSE:
        return tg
    key = norm_key(key_raw)
    if not key:
        raise ApiError(401, "KEY_REQUIRED", "License key is required")
    row = q_one(cur, "SELECT telegram_id, revoked FROM sg_licenses WHERE license_key = %s", (key,))
    if not row:
        raise ApiError(403, "KEY_NOT_FOUND", "License key not found")
    if row["revoked"]:
        raise ApiError(403, "KEY_REVOKED", "License key was revoked")
    if str(row["telegram_id"] or "") != tg:
        raise ApiError(403, "KEY_MISMATCH", "License key belongs to another account")
    return tg


def settle(cur, tg: str, game: str, bet: int, payout: int, outcome: str, deduct: int) -> Optional[int]:
    """Завершает игру одним атомарным запросом: списывает `deduct`, начисляет `payout`,
    обновляет статистику и пишет историю. Возвращает новый баланс или None, если не хватило монет."""
    t = now()
    win = 1 if payout > bet else 0
    row = q_one(
        cur,
        "UPDATE sg_users SET coins = coins - %s + %s, games_played = games_played + 1, "
        "wins = wins + %s, total_wagered = total_wagered + %s, total_won = total_won + %s, "
        "updated_at = %s WHERE telegram_id = %s AND coins >= %s RETURNING coins",
        (deduct, payout, win, bet, payout, t, tg, deduct))
    if not row:
        return None
    cur.execute(
        "INSERT INTO sg_game_history (id, telegram_id, game, bet, payout, net, outcome, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (new_id(), tg, game, bet, payout, payout - bet, outcome, t))
    return int(row["coins"])


# ───────────────────────────── лицензии ─────────────────────────────

def svc_activate(cur, key_raw: str, tg_raw: str) -> Dict[str, Any]:
    key = norm_key(key_raw)
    tg = check_tg(tg_raw)
    if not key:
        raise ApiError(400, "INVALID_REQUEST", "Invalid key or Telegram ID")
    row = q_one(cur, "SELECT telegram_id, revoked FROM sg_licenses WHERE license_key = %s", (key,))
    if not row:
        raise ApiError(404, "KEY_NOT_FOUND", "License key not found")
    if row["revoked"]:
        raise ApiError(403, "KEY_REVOKED", "License key was revoked")
    owner = str(row["telegram_id"] or "")
    if owner and owner != tg:
        raise ApiError(409, "KEY_ALREADY_USED", "License key is bound to another account")
    t = now()
    if not owner:
        cur.execute(
            "UPDATE sg_licenses SET telegram_id = %s, activated_at = %s, last_check_at = %s "
            "WHERE license_key = %s AND telegram_id IS NULL", (tg, t, t, key))
        if cur.rowcount == 0:  # кто-то успел раньше
            again = q_one(cur, "SELECT telegram_id FROM sg_licenses WHERE license_key = %s", (key,))
            if not again or str(again["telegram_id"] or "") != tg:
                raise ApiError(409, "KEY_ALREADY_USED", "License key is bound to another account")
    else:
        cur.execute("UPDATE sg_licenses SET last_check_at = %s WHERE license_key = %s", (t, key))
    ensure_user(cur, tg)
    return {"ok": True, "status": "active", "telegram_id": tg, "coins": get_coins(cur, tg)}


# ───────────────────────────── баланс / кошелёк ─────────────────────────────

def licensed_rank(cur, tg: str, coins: int) -> Optional[int]:
    has_license = q_one(
        cur, "SELECT 1 AS x FROM sg_licenses WHERE telegram_id = %s AND revoked = FALSE", (tg,))
    if not has_license:
        return None
    row = q_one(
        cur,
        "SELECT COUNT(*) AS n FROM sg_users u WHERE u.coins > %s AND EXISTS "
        "(SELECT 1 FROM sg_licenses l WHERE l.telegram_id = u.telegram_id AND l.revoked = FALSE)", (coins,))
    return int(row["n"]) + 1


def svc_balance(cur, tg_raw: str) -> Dict[str, Any]:
    tg = check_tg(tg_raw)
    row = q_one(cur, "SELECT coins FROM sg_users WHERE telegram_id = %s", (tg,))
    if not row:
        return {"ok": True, "telegram_id": tg, "coins": START_COINS, "exists": False}
    return {"ok": True, "telegram_id": tg, "coins": int(row["coins"]), "exists": True}


def svc_profile(cur, tg_raw: str) -> Dict[str, Any]:
    data = svc_balance(cur, tg_raw)
    data["rank"] = licensed_rank(cur, data["telegram_id"], data["coins"]) if data["exists"] else None
    return data


def svc_leaderboard(cur, limit: int) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    rows = q_all(
        cur,
        "SELECT u.telegram_id, u.coins FROM sg_users u WHERE EXISTS "
        "(SELECT 1 FROM sg_licenses l WHERE l.telegram_id = u.telegram_id AND l.revoked = FALSE) "
        "ORDER BY u.coins DESC, u.telegram_id ASC LIMIT %s", (limit,))
    top = [{"rank": i + 1, "telegram_id": r["telegram_id"], "coins": int(r["coins"])}
           for i, r in enumerate(rows)]
    return {"ok": True, "top": top}


def svc_daily(cur, tg_raw: str, key: Any) -> Dict[str, Any]:
    tg = require_license(cur, tg_raw, key)
    ensure_user(cur, tg)
    t = now()
    reward = rng.randint(DAILY_MIN, DAILY_MAX)
    row = q_one(
        cur,
        "UPDATE sg_users SET coins = coins + %s, last_daily = %s, updated_at = %s "
        "WHERE telegram_id = %s AND last_daily <= %s RETURNING coins",
        (reward, t, t, tg, t - DAILY_COOLDOWN))
    if not row:
        last = q_one(cur, "SELECT last_daily FROM sg_users WHERE telegram_id = %s", (tg,))
        wait = max(1, int(last["last_daily"]) + DAILY_COOLDOWN - t) if last else DAILY_COOLDOWN
        raise ApiError(429, "DAILY_COOLDOWN",
                       {"message": "Daily bonus already claimed", "retry_after_seconds": wait})
    log_coins(cur, tg, reward, "daily")
    return {"ok": True, "reward": reward, "coins": int(row["coins"]),
            "cooldown_seconds": DAILY_COOLDOWN}


def svc_transfer(cur, sender_raw: str, recipient_raw: str, amount: Any, key: Any) -> Dict[str, Any]:
    sender = require_license(cur, sender_raw, key)
    recipient = check_tg(recipient_raw)
    if sender == recipient:
        raise ApiError(400, "SAME_ACCOUNT", "Cannot transfer to the same account")
    amount = check_bet(amount, MAX_TRANSFER)
    ensure_user(cur, sender)
    ensure_user(cur, recipient)
    # блокируем обе строки в одном порядке, чтобы встречные переводы не создавали взаимную блокировку
    q_all(cur, "SELECT telegram_id FROM sg_users WHERE telegram_id IN (%s, %s) "
               "ORDER BY telegram_id FOR UPDATE", (sender, recipient))
    t = now()
    row = q_one(cur, "UPDATE sg_users SET coins = coins - %s, updated_at = %s "
                     "WHERE telegram_id = %s AND coins >= %s RETURNING coins",
                (amount, t, sender, amount))
    if not row:
        raise insufficient(get_coins(cur, sender))
    got = q_one(cur, "UPDATE sg_users SET coins = coins + %s, updated_at = %s "
                     "WHERE telegram_id = %s RETURNING coins", (amount, t, recipient))
    log_coins(cur, sender, -amount, "transfer_out", "to " + recipient)
    log_coins(cur, recipient, amount, "transfer_in", "from " + sender)
    return {"ok": True, "amount": amount, "sender_balance": int(row["coins"]),
            "recipient_balance": int(got["coins"]), "recipient_id": recipient}


# ───────────────────────────── игры ─────────────────────────────

def svc_play(cur, tg_raw: str, key: Any, game_raw: Any, amount: Any) -> Dict[str, Any]:
    tg = require_license(cur, tg_raw, key)
    game = INSTANT_ALIASES.get(str(game_raw or "").strip().lower())
    if not game:
        raise ApiError(400, "UNKNOWN_GAME", "Unknown game. Use fut, bas or casino")
    bet = check_bet(amount)
    ensure_user(cur, tg)
    roll = rng.random()
    acc = 0.0
    outcome, mult = INSTANT_GAMES[game][-1][0], INSTANT_GAMES[game][-1][2]
    for name, prob, m in INSTANT_GAMES[game]:
        acc += prob
        if roll < acc:
            outcome, mult = name, m
            break
    payout = int(bet * mult + 1e-9)
    coins = settle(cur, tg, game, bet, payout, outcome, bet)
    if coins is None:
        raise insufficient(get_coins(cur, tg))
    return {"ok": True, "game": game, "outcome": outcome, "bet": bet, "multiplier": mult,
            "payout": payout, "net": payout - bet, "coins": coins}


def round_game(game_raw: Any) -> str:
    game = str(game_raw or "").strip().lower()
    if game not in ROUND_GAMES:
        raise ApiError(400, "UNKNOWN_GAME", "Unknown round game. Use mines, tower, roc or casino")
    return game


def round_mult(game: str, steps: int) -> float:
    if steps <= 0:
        return 1.0
    return round(min(MAX_MULT, ROUND_RTP / (ROUND_GAMES[game]["p"] ** steps)), 2)


def svc_start(cur, tg_raw: str, key: Any, game_raw: Any, amount: Any) -> Dict[str, Any]:
    tg = require_license(cur, tg_raw, key)
    game = round_game(game_raw)
    bet = check_bet(amount)
    ensure_user(cur, tg)
    t = now()
    cur.execute("DELETE FROM sg_rounds WHERE telegram_id = %s AND game = %s AND created_at < %s",
                (tg, game, t - ROUND_TTL))
    if q_one(cur, "SELECT 1 AS x FROM sg_rounds WHERE telegram_id = %s AND game = %s", (tg, game)):
        raise ApiError(409, "ROUND_ACTIVE",
                       "У тебя уже есть активный раунд. Продолжить: /%s 1, забрать: /%s 2" % (game, game))
    row = q_one(cur, "UPDATE sg_users SET coins = coins - %s, updated_at = %s "
                     "WHERE telegram_id = %s AND coins >= %s RETURNING coins", (bet, t, tg, bet))
    if not row:
        raise insufficient(get_coins(cur, tg))
    cur.execute("INSERT INTO sg_rounds (telegram_id, game, bet, steps, created_at) VALUES (%s,%s,%s,0,%s) "
                "ON CONFLICT (telegram_id, game) DO NOTHING", (tg, game, bet, t))
    if cur.rowcount == 0:
        raise ApiError(409, "ROUND_ACTIVE", "Round is already active")
    return {"ok": True, "game": game, "bet": bet, "steps": 0, "multiplier": 1.0,
            "coins": int(row["coins"])}


def _load_round(cur, tg: str, game: str):
    rnd = q_one(cur, "SELECT bet, steps, created_at FROM sg_rounds WHERE telegram_id = %s AND game = %s "
                     "FOR UPDATE", (tg, game))
    if not rnd:
        raise ApiError(404, "ROUND_NOT_FOUND", "Раунд не найден. Начни новый: /%s СТАВКА" % game)
    if now() - int(rnd["created_at"]) > ROUND_TTL:
        cur.execute("DELETE FROM sg_rounds WHERE telegram_id = %s AND game = %s", (tg, game))
        bet = int(rnd["bet"])
        settle(cur, tg, game, bet, 0, "expired", 0)
        return None
    return rnd


def _finish_cashout(cur, tg: str, game: str, bet: int, steps: int) -> Dict[str, Any]:
    mult = round_mult(game, steps)
    payout = int(bet * mult + 1e-9)
    cur.execute("DELETE FROM sg_rounds WHERE telegram_id = %s AND game = %s", (tg, game))
    coins = settle(cur, tg, game, bet, payout, "cashout", 0)
    if coins is None:
        raise ApiError(500, "SETTLE_FAILED", "Could not settle round")
    return {"ok": True, "game": game, "outcome": "cashout", "bet": bet, "steps": steps,
            "multiplier": mult, "payout": payout, "net": payout - bet, "coins": coins}


def svc_step(cur, tg_raw: str, key: Any, game_raw: Any) -> Dict[str, Any]:
    tg = require_license(cur, tg_raw, key)
    game = round_game(game_raw)
    rnd = _load_round(cur, tg, game)
    if rnd is None:
        return {"ok": False, "error": "ROUND_EXPIRED",
                "detail": "Раунд сгорел из-за долгого бездействия. Начни новый."}
    bet, steps = int(rnd["bet"]), int(rnd["steps"])
    cfg = ROUND_GAMES[game]
    if rng.random() >= cfg["p"]:
        cur.execute("DELETE FROM sg_rounds WHERE telegram_id = %s AND game = %s", (tg, game))
        coins = settle(cur, tg, game, bet, 0, "loss", 0)
        return {"ok": True, "game": game, "outcome": "loss", "reason": cfg["bad"], "bet": bet,
                "steps": steps, "payout": 0, "net": -bet, "coins": coins}
    steps += 1
    mult = round_mult(game, steps)
    if steps >= MAX_STEPS or mult >= MAX_MULT:
        return _finish_cashout(cur, tg, game, bet, steps)
    cur.execute("UPDATE sg_rounds SET steps = %s, created_at = %s WHERE telegram_id = %s AND game = %s",
                (steps, now(), tg, game))
    return {"ok": True, "game": game, "outcome": cfg["ok"], "bet": bet, "steps": steps,
            "multiplier": mult, "cashout": int(bet * mult + 1e-9), "coins": get_coins(cur, tg)}


def svc_cashout(cur, tg_raw: str, key: Any, game_raw: Any) -> Dict[str, Any]:
    tg = require_license(cur, tg_raw, key)
    game = round_game(game_raw)
    rnd = _load_round(cur, tg, game)
    if rnd is None:
        return {"ok": False, "error": "ROUND_EXPIRED",
                "detail": "Раунд сгорел из-за долгого бездействия. Начни новый."}
    if int(rnd["steps"]) < 1:
        raise ApiError(400, "NO_STEPS", "Сначала сделай хотя бы один успешный шаг (1).")
    return _finish_cashout(cur, tg, game, int(rnd["bet"]), int(rnd["steps"]))


def svc_roulette(cur, tg_raw: str, key: Any, amount: Any, choice_raw: Any) -> Dict[str, Any]:
    tg = require_license(cur, tg_raw, key)
    bet = check_bet(amount)
    choice = str(choice_raw or "").strip().lower()
    choice = CHOICE_ALIASES.get(choice, choice)
    if choice not in ("red", "black") and not (choice.isdigit() and 0 <= int(choice) <= 36):
        raise ApiError(400, "BAD_CHOICE", "Choice must be red, black or a number 0-36")
    ensure_user(cur, tg)
    number = rng.randint(0, 36)
    color = "green" if number == 0 else ("red" if number in RED_NUMBERS else "black")
    if choice in ("red", "black"):
        won, mult = (color == choice), 2
    else:
        won, mult = (number == int(choice)), 36
    payout = bet * mult if won else 0
    coins = settle(cur, tg, "roulette", bet, payout, "win" if won else "loss", bet)
    if coins is None:
        raise insufficient(get_coins(cur, tg))
    return {"ok": True, "number": number, "color": color, "choice": choice, "won": won,
            "bet": bet, "multiplier": mult if won else 0, "payout": payout,
            "net": payout - bet, "coins": coins}


def svc_stats(cur, tg_raw: str, key: Any) -> Dict[str, Any]:
    tg = require_license(cur, tg_raw, key)
    ensure_user(cur, tg)
    u = q_one(cur, "SELECT coins, games_played, wins, total_wagered, total_won FROM sg_users "
                   "WHERE telegram_id = %s", (tg,))
    recent = q_all(cur, "SELECT game, bet, payout, net, outcome, created_at FROM sg_game_history "
                        "WHERE telegram_id = %s ORDER BY created_at DESC, id DESC LIMIT 10", (tg,))
    played, wins = int(u["games_played"]), int(u["wins"])
    return {"ok": True, "telegram_id": tg, "coins": int(u["coins"]), "games_played": played,
            "wins": wins, "losses": max(0, played - wins),
            "total_wagered": int(u["total_wagered"]), "total_won": int(u["total_won"]),
            "net_total": int(u["total_won"]) - int(u["total_wagered"]),
            "rank": licensed_rank(cur, tg, int(u["coins"])), "recent": recent}


# ───────────────────────────── NFT ─────────────────────────────

def svc_nft_sync(cur, tg_raw: str, key: Any, items: list, replace_all: bool) -> Dict[str, Any]:
    tg = require_license(cur, tg_raw, key)
    if len(items) > MAX_NFT_ITEMS:
        raise ApiError(413, "TOO_MANY_ITEMS", "Send at most %d items per request" % MAX_NFT_ITEMS)
    t = now()
    if replace_all:
        cur.execute("DELETE FROM sg_nft_items WHERE publisher_id = %s", (tg,))
    stored = 0
    for it in items:
        dialog = str(it.dialog_id or "").strip()
        gift = (it.gift or "").replace("\x00", "")
        if not dialog.lstrip("-").isdigit() or not gift:
            continue
        if len(gift) > MAX_GIFT_CHARS:
            continue
        item_key = str(it.key or "")[:300] or ("gift-" + hashlib.sha1(gift.encode("utf-8")).hexdigest()[:16])
        cur.execute(
            "INSERT INTO sg_nft_items (publisher_id, dialog_id, item_key, gift, from_id, anonymous, "
            "message, pin, title, slug, ts, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (publisher_id, dialog_id, item_key) DO UPDATE SET gift = EXCLUDED.gift, "
            "from_id = EXCLUDED.from_id, anonymous = EXCLUDED.anonymous, message = EXCLUDED.message, "
            "pin = EXCLUDED.pin, title = EXCLUDED.title, slug = EXCLUDED.slug, ts = EXCLUDED.ts, "
            "updated_at = EXCLUDED.updated_at",
            (tg, dialog, item_key, gift, str(it.from_id or "0")[:20], bool(it.anonymous),
             str(it.message or "").replace("\x00", "")[:400], bool(it.pin), str(it.title or "").replace("\x00", "")[:120],
             str(it.slug or "").replace("\x00", "")[:120], float(it.ts or t), t))
        stored += 1
    total = q_one(cur, "SELECT COUNT(*) AS n FROM sg_nft_items WHERE publisher_id = %s", (tg,))
    return {"ok": True, "stored": stored, "total": int(total["n"])}


def svc_nft_lookup(cur, tg_raw: str, key: Any, targets: list) -> Dict[str, Any]:
    require_license(cur, tg_raw, key)
    users: Dict[str, list] = {}
    for raw in targets[:50]:
        target = str(raw or "").strip()
        if not target.lstrip("-").isdigit():
            continue
        rows = q_all(
            cur,
            "SELECT publisher_id, dialog_id, item_key, gift, from_id, anonymous, message, pin, title, "
            "slug, ts FROM sg_nft_items WHERE dialog_id = %s ORDER BY updated_at DESC, ts DESC LIMIT 300",
            (target,))
        seen, out = set(), []
        for r in rows:
            if r["item_key"] in seen:
                continue
            seen.add(r["item_key"])
            out.append({
                "gift": r["gift"], "publisher_id": int(r["publisher_id"]),
                "dialog_id": int(r["dialog_id"]), "key": r["item_key"],
                "from_id": int(r["from_id"] or 0) if str(r["from_id"] or "0").lstrip("-").isdigit() else 0,
                "anonymous": bool(r["anonymous"]), "message": r["message"], "pin": bool(r["pin"]),
                "title": r["title"], "slug": r["slug"], "ts": float(r["ts"] or 0)})
            if len(out) >= 100:
                break
        users[target] = out
    return {"ok": True, "users": users}


# ───────────────────────────── админка ─────────────────────────────

def admin_generate(cur, amount: int, note: str) -> Dict[str, Any]:
    if amount < 1 or amount > 500:
        raise ApiError(400, "BAD_AMOUNT", "amount must be from 1 to 500")
    t = now()
    keys = []
    for _ in range(amount):
        key = make_key()
        cur.execute("INSERT INTO sg_licenses (license_key, telegram_id, revoked, note, created_at) "
                    "VALUES (%s, NULL, FALSE, %s, %s)", (key, (note or "")[:200], t))
        keys.append(key)
    return {"ok": True, "count": len(keys), "keys": keys}


def admin_generate_for_user(cur, tg_raw: str, note: str) -> Dict[str, Any]:
    tg = check_tg(tg_raw)
    t = now()
    key = make_key()
    cur.execute("INSERT INTO sg_licenses (license_key, telegram_id, revoked, note, created_at, activated_at) "
                "VALUES (%s, %s, FALSE, %s, %s, %s)", (key, tg, (note or "")[:200], t, t))
    ensure_user(cur, tg)
    return {"ok": True, "key": key, "telegram_id": tg}


def admin_bind(cur, key_raw: str, tg_raw: str) -> Dict[str, Any]:
    key = norm_key(key_raw)
    tg = check_tg(tg_raw)
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9\-_]{5,63}", key):
        raise ApiError(400, "BAD_KEY", "Key must be 6-64 chars: A-Z, 0-9, '-' or '_'")
    t = now()
    row = q_one(cur, "SELECT telegram_id FROM sg_licenses WHERE license_key = %s", (key,))
    if row:
        cur.execute("UPDATE sg_licenses SET telegram_id = %s, activated_at = COALESCE(activated_at, %s) "
                    "WHERE license_key = %s", (tg, t, key))
        action = "rebound" if row["telegram_id"] and row["telegram_id"] != tg else "bound"
    else:
        cur.execute("INSERT INTO sg_licenses (license_key, telegram_id, revoked, note, created_at, activated_at) "
                    "VALUES (%s, %s, FALSE, %s, %s, %s)", (key, tg, "imported", t, t))
        action = "created"
    ensure_user(cur, tg)
    return {"ok": True, "key": key, "telegram_id": tg, "action": action}


def admin_set_revoked(cur, key_raw: str, revoked: bool) -> Dict[str, Any]:
    key = norm_key(key_raw)
    cur.execute("UPDATE sg_licenses SET revoked = %s WHERE license_key = %s", (revoked, key))
    if cur.rowcount == 0:
        raise ApiError(404, "KEY_NOT_FOUND", "License key not found")
    return {"ok": True, "key": key, "revoked": revoked}


def admin_set_revoked_user(cur, tg_raw: str, revoked: bool) -> Dict[str, Any]:
    tg = check_tg(tg_raw)
    cur.execute("UPDATE sg_licenses SET revoked = %s WHERE telegram_id = %s", (revoked, tg))
    return {"ok": True, "telegram_id": tg, "revoked": revoked, "keys_changed": cur.rowcount}


def admin_list_licenses(cur, telegram_id: Optional[str], status: Optional[str],
                        limit: int, offset: int) -> Dict[str, Any]:
    where, params = [], []
    if telegram_id:
        where.append("telegram_id = %s")
        params.append(check_tg(telegram_id))
    if status == "unused":
        where.append("telegram_id IS NULL AND revoked = FALSE")
    elif status == "active":
        where.append("telegram_id IS NOT NULL AND revoked = FALSE")
    elif status == "revoked":
        where.append("revoked = TRUE")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = q_one(cur, "SELECT COUNT(*) AS n FROM sg_licenses" + clause, tuple(params))
    rows = q_all(
        cur,
        "SELECT license_key, telegram_id, revoked, note, created_at, activated_at, last_check_at "
        "FROM sg_licenses" + clause + " ORDER BY created_at DESC, license_key ASC LIMIT %s OFFSET %s",
        tuple(params) + (max(1, min(limit, 1000)), max(0, offset)))
    for r in rows:
        r["revoked"] = bool(r["revoked"])
    return {"ok": True, "total": int(total["n"]), "licenses": rows}


def admin_user(cur, tg_raw: str) -> Dict[str, Any]:
    tg = check_tg(tg_raw)
    licenses = q_all(cur, "SELECT license_key, revoked, note, created_at, activated_at, last_check_at "
                          "FROM sg_licenses WHERE telegram_id = %s ORDER BY created_at DESC", (tg,))
    for r in licenses:
        r["revoked"] = bool(r["revoked"])
    user = q_one(cur, "SELECT coins, last_daily, games_played, wins, total_wagered, total_won, created_at "
                      "FROM sg_users WHERE telegram_id = %s", (tg,))
    nft = q_one(cur, "SELECT COUNT(*) AS n FROM sg_nft_items WHERE publisher_id = %s", (tg,))
    shown = q_one(cur, "SELECT COUNT(*) AS n FROM sg_nft_items WHERE dialog_id = %s", (tg,))
    log_rows = q_all(cur, "SELECT delta, reason, note, created_at FROM sg_coin_log WHERE telegram_id = %s "
                          "ORDER BY created_at DESC, id DESC LIMIT 10", (tg,))
    return {"ok": True, "telegram_id": tg, "licenses": licenses, "account": user,
            "nft_published": int(nft["n"]), "nft_on_profile": int(shown["n"]), "coin_log": log_rows}


def admin_balance_add(cur, tg_raw: str, amount: int, note: str) -> Dict[str, Any]:
    tg = check_tg(tg_raw)
    if amount == 0 or abs(amount) > MAX_ADMIN_DELTA:
        raise ApiError(400, "BAD_AMOUNT", "amount must be non-zero and within +/-%d" % MAX_ADMIN_DELTA)
    ensure_user(cur, tg)
    before = q_one(cur, "SELECT coins FROM sg_users WHERE telegram_id = %s FOR UPDATE", (tg,))
    old = int(before["coins"])
    new = max(0, old + amount)
    if new > MAX_COINS:
        raise ApiError(400, "BALANCE_TOO_LARGE", "Resulting balance is too large")
    cur.execute("UPDATE sg_users SET coins = %s, updated_at = %s WHERE telegram_id = %s", (new, now(), tg))
    log_coins(cur, tg, new - old, "admin_add", note)
    return {"ok": True, "telegram_id": tg, "added": new - old, "old_balance": old, "coins": new}


def admin_balance_set(cur, tg_raw: str, coins: int, note: str) -> Dict[str, Any]:
    tg = check_tg(tg_raw)
    if coins < 0 or coins > MAX_COINS:
        raise ApiError(400, "BAD_AMOUNT", "coins must be from 0 to %d" % MAX_COINS)
    ensure_user(cur, tg)
    before = q_one(cur, "SELECT coins FROM sg_users WHERE telegram_id = %s FOR UPDATE", (tg,))
    old = int(before["coins"])
    cur.execute("UPDATE sg_users SET coins = %s, updated_at = %s WHERE telegram_id = %s", (coins, now(), tg))
    log_coins(cur, tg, coins - old, "admin_set", note)
    return {"ok": True, "telegram_id": tg, "old_balance": old, "coins": coins}


def admin_totals(cur) -> Dict[str, Any]:
    def n(sql):
        return int(q_one(cur, sql)["n"])
    return {"ok": True,
            "licenses_total": n("SELECT COUNT(*) AS n FROM sg_licenses"),
            "licenses_active": n("SELECT COUNT(*) AS n FROM sg_licenses WHERE telegram_id IS NOT NULL AND revoked = FALSE"),
            "licenses_unused": n("SELECT COUNT(*) AS n FROM sg_licenses WHERE telegram_id IS NULL AND revoked = FALSE"),
            "licenses_revoked": n("SELECT COUNT(*) AS n FROM sg_licenses WHERE revoked = TRUE"),
            "users": n("SELECT COUNT(*) AS n FROM sg_users"),
            "nft_items": n("SELECT COUNT(*) AS n FROM sg_nft_items"),
            "games_played": n("SELECT COUNT(*) AS n FROM sg_game_history")}


# ───────────────────────────── модели запросов ─────────────────────────────

def _to_str(value):
    if value is None:
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


Tg = Annotated[str, BeforeValidator(_to_str)]


class ActivateIn(BaseModel):
    key: str
    telegram_id: Tg


class AuthIn(BaseModel):
    telegram_id: Optional[Tg] = None
    key: Optional[str] = None


class GameIn(BaseModel):
    telegram_id: Tg
    key: Optional[str] = None
    amount: int
    game: Optional[str] = None


class RouletteIn(BaseModel):
    telegram_id: Tg
    key: Optional[str] = None
    amount: int
    choice: str


class TransferIn(BaseModel):
    sender_id: Tg
    recipient_id: Tg
    amount: int
    key: Optional[str] = None


class StatsIn(BaseModel):
    telegram_id: Tg
    key: Optional[str] = None


class NftItem(BaseModel):
    dialog_id: Tg
    key: Optional[str] = ""
    gift: str
    from_id: Optional[Tg] = "0"
    anonymous: Optional[bool] = False
    message: Optional[str] = ""
    pin: Optional[bool] = False
    title: Optional[str] = ""
    slug: Optional[str] = ""
    ts: Optional[float] = 0


class NftSyncIn(BaseModel):
    telegram_id: Tg
    key: Optional[str] = None
    items: List[NftItem] = []
    replace_all: bool = False


class NftLookupIn(BaseModel):
    telegram_id: Tg
    key: Optional[str] = None
    targets: List[Tg] = []


class GenerateIn(BaseModel):
    amount: int = 1
    note: Optional[str] = ""


class GenerateForUserIn(BaseModel):
    telegram_id: Tg
    note: Optional[str] = ""


class BindIn(BaseModel):
    key: str
    telegram_id: Tg


class KeyIn(BaseModel):
    key: str


class UserIn(BaseModel):
    telegram_id: Tg


class BalanceAddIn(BaseModel):
    telegram_id: Tg
    amount: int
    note: Optional[str] = ""


class BalanceSetIn(BaseModel):
    telegram_id: Tg
    coins: int
    note: Optional[str] = ""


# ───────────────────────────── приложение ─────────────────────────────

@asynccontextmanager
async def lifespan(_app):
    try:
        run(lambda cur: None)  # создаёт таблицы
        log.info("database ready")
    except Exception as exc:  # сервер всё равно стартует, /health покажет проблему
        log.error("database is not ready yet: %s", exc)
    yield


app = FastAPI(
    title="SuperGram API", version=VERSION, lifespan=lifespan,
    description="Лицензии, NFT, глобальный баланс и игры (виртуальные очки). "
                "Все /admin/* требуют заголовок X-Admin-Secret.")


@app.exception_handler(ApiError)
async def api_error_handler(_request, exc: ApiError):
    body = {"ok": False, "error": exc.code, "detail": exc.detail}
    body.update(exc.extra)
    return JSONResponse(status_code=exc.status, content=body)


@app.exception_handler(RequestValidationError)
async def validation_handler(_request, exc: RequestValidationError):
    first = (exc.errors() or [{}])[0]
    where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    return JSONResponse(status_code=422, content={
        "ok": False, "error": "VALIDATION_ERROR",
        "detail": ("%s: %s" % (where, first.get("msg", "invalid request"))).strip(": ")})


@app.exception_handler(Exception)
async def unhandled_handler(_request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={
        "ok": False, "error": "SERVER_ERROR: " + str(exc)[:100], "detail": str(exc)[:300]})


STATUS_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SuperGram API</title>
<style>body{{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;display:grid;
place-items:center;min-height:100vh;margin:0}}main{{max-width:30rem;padding:1.5rem}}
.ok{{color:#3fb950}}.bad{{color:#f85149}}a{{color:#58a6ff}}</style></head><body><main>
<h1>SuperGram API</h1><p>Версия {version}</p><p>База данных: <b class="{cls}">{db}</b></p>
<p><a href="/docs">Документация</a> · <a href="/health">/health</a></p></main></body></html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def status_page():
    try:
        run(lambda cur: q_one(cur, "SELECT 1 AS ok"))
        db, cls = "работает", "ok"
    except Exception:
        db, cls = "недоступна", "bad"
    return HTMLResponse(STATUS_PAGE.format(version=VERSION, db=db, cls=cls))


@app.get("/health")
def health():
    try:
        run(lambda cur: q_one(cur, "SELECT 1 AS ok"))
        return {"ok": True, "db": "up", "version": VERSION}
    except ApiError as exc:
        return JSONResponse(status_code=503, content={
            "ok": False, "db": "down", "error": exc.code, "detail": exc.detail})
    except Exception as exc:
        return JSONResponse(status_code=503, content={
            "ok": False, "db": "down", "error": "DB_ERROR", "detail": str(exc)[:200]})


# ── лицензии ──

@app.post("/activate")
def activate(body: ActivateIn):
    return run(lambda cur: svc_activate(cur, body.key, body.telegram_id))


# ── кошелёк ──

@app.get("/balance/{telegram_id}")
def balance(telegram_id: str):
    return run(lambda cur: svc_balance(cur, telegram_id))


@app.get("/profile/{telegram_id}")
def profile(telegram_id: str):
    return run(lambda cur: svc_profile(cur, telegram_id))


@app.get("/top")
def top(limit: int = Query(10, ge=1, le=100)):
    return run(lambda cur: svc_leaderboard(cur, limit))


@app.post("/daily/{telegram_id}")
def daily(telegram_id: str, body: Optional[AuthIn] = None):
    return run(lambda cur: svc_daily(cur, telegram_id, body.key if body else None))


@app.post("/transfer")
def transfer(body: TransferIn):
    return run(lambda cur: svc_transfer(cur, body.sender_id, body.recipient_id, body.amount, body.key))


# ── игры ──

@app.post("/game/start")
def game_start(body: GameIn):
    return run(lambda cur: svc_start(cur, body.telegram_id, body.key, body.game, body.amount))


@app.post("/game/play")
def game_play(body: GameIn):
    return run(lambda cur: svc_play(cur, body.telegram_id, body.key, body.game, body.amount))


@app.post("/game/step/{game}")
def game_step(game: str, telegram_id: str = Query(...), body: Optional[AuthIn] = None):
    return run(lambda cur: svc_step(cur, telegram_id, body.key if body else None, game))


@app.post("/game/cashout/{game}")
def game_cashout(game: str, telegram_id: str = Query(...), body: Optional[AuthIn] = None):
    return run(lambda cur: svc_cashout(cur, telegram_id, body.key if body else None, game))


@app.post("/game/{game}")
def game_instant(game: str, body: GameIn):
    return run(lambda cur: svc_play(cur, body.telegram_id, body.key, game, body.amount))


@app.post("/roulette")
def roulette(body: RouletteIn):
    return run(lambda cur: svc_roulette(cur, body.telegram_id, body.key, body.amount, body.choice))


# ── статистика ──

@app.post("/stats/me")
def stats_me(body: StatsIn):
    return run(lambda cur: svc_stats(cur, body.telegram_id, body.key))


@app.get("/stats/leaderboard")
def stats_leaderboard(limit: int = Query(10, ge=1, le=100)):
    return run(lambda cur: svc_leaderboard(cur, limit))


# ── NFT ──

@app.post("/nft/sync")
def nft_sync(body: NftSyncIn):
    return run(lambda cur: svc_nft_sync(cur, body.telegram_id, body.key, body.items, body.replace_all))


@app.post("/nft/lookup")
def nft_lookup(body: NftLookupIn):
    return run(lambda cur: svc_nft_lookup(cur, body.telegram_id, body.key, body.targets))


# ── админка ──

def admin_guard(x_admin_secret: Optional[str] = Header(default=None)):
    if not ADMIN_SECRET:
        raise ApiError(503, "ADMIN_DISABLED", "ADMIN_SECRET is not set on the server")
    given = (x_admin_secret or "").encode("utf-8")
    if not hmac.compare_digest(given, ADMIN_SECRET.encode("utf-8")):
        raise ApiError(403, "FORBIDDEN", "Invalid admin secret")


admin = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(admin_guard)])


@admin.get("/stats")
def admin_stats():
    return run(admin_totals)


@admin.post("/generate")
def admin_generate_route(body: GenerateIn):
    return run(lambda cur: admin_generate(cur, body.amount, body.note or ""))


@admin.post("/generate-for-user")
def admin_generate_for_user_route(body: GenerateForUserIn):
    return run(lambda cur: admin_generate_for_user(cur, body.telegram_id, body.note or ""))


@admin.get("/licenses")
def admin_licenses_list(telegram_id: Optional[str] = None, status: Optional[str] = None,
                        limit: int = 100, offset: int = 0):
    return run(lambda cur: admin_list_licenses(cur, telegram_id, status, limit, offset))


@admin.post("/licenses")
def admin_licenses_bind(body: BindIn):
    return run(lambda cur: admin_bind(cur, body.key, body.telegram_id))


@admin.post("/revoke")
def admin_revoke(body: KeyIn):
    return run(lambda cur: admin_set_revoked(cur, body.key, True))


@admin.post("/unrevoke")
def admin_unrevoke(body: KeyIn):
    return run(lambda cur: admin_set_revoked(cur, body.key, False))


@admin.post("/revoke-user")
def admin_revoke_user(body: UserIn):
    return run(lambda cur: admin_set_revoked_user(cur, body.telegram_id, True))


@admin.post("/unrevoke-user")
def admin_unrevoke_user(body: UserIn):
    return run(lambda cur: admin_set_revoked_user(cur, body.telegram_id, False))


@admin.get("/user")
def admin_user_get(telegram_id: str):
    return run(lambda cur: admin_user(cur, telegram_id))


@admin.post("/user")
def admin_user_post(body: UserIn):
    return run(lambda cur: admin_user(cur, body.telegram_id))


@admin.get("/balance/{telegram_id}")
def admin_balance_get(telegram_id: str):
    return run(lambda cur: svc_balance(cur, telegram_id))


@admin.post("/balance/add")
def admin_balance_add_route(body: BalanceAddIn):
    return run(lambda cur: admin_balance_add(cur, body.telegram_id, body.amount, body.note or ""))


@admin.post("/add-balance", include_in_schema=False)
def admin_balance_add_alias(body: BalanceAddIn):
    return run(lambda cur: admin_balance_add(cur, body.telegram_id, body.amount, body.note or ""))


@admin.post("/balance/set")
def admin_balance_set_route(body: BalanceSetIn):
    return run(lambda cur: admin_balance_set(cur, body.telegram_id, body.coins, body.note or ""))


app.include_router(admin)
