import os, secrets, string, time
from contextlib import contextmanager
from urllib.parse import urlparse
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("DB_PATH", "supergrammod.db")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
app = FastAPI(title="SuperGrammod Backend", version="1.2.0")

def is_postgres():
    return DATABASE_URL.startswith(("postgres://", "postgresql://"))

@contextmanager
def db():
    if is_postgres():
        import psycopg
        from psycopg.rows import dict_row
        # Neon URLs commonly use sslmode=require; preserve provided query params.
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        import sqlite3
        conn = sqlite3.connect(DB_PATH, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def q(sql):
    # The API uses ? placeholders. Translate for psycopg.
    return sql.replace("?", "%s") if is_postgres() else sql

def execute(conn, sql, params=()):
    return conn.execute(q(sql), params)

def init_db():
    with db() as c:
        if is_postgres():
            c.execute("""
                CREATE TABLE IF NOT EXISTS users(
                    telegram_id TEXT PRIMARY KEY,
                    coins BIGINT NOT NULL DEFAULT 0 CHECK(coins >= 0),
                    last_daily BIGINT NOT NULL DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS licenses(
                    license_key TEXT PRIMARY KEY,
                    telegram_id TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at BIGINT NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS transfers(
                    id BIGSERIAL PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    amount BIGINT NOT NULL,
                    created_at BIGINT NOT NULL
                )
            """)
        else:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users(telegram_id TEXT PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 0 CHECK(coins>=0), last_daily INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS licenses(license_key TEXT PRIMARY KEY, telegram_id TEXT, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY AUTOINCREMENT,sender_id TEXT NOT NULL,recipient_id TEXT NOT NULL,amount INTEGER NOT NULL,created_at INTEGER NOT NULL);
            """)

@app.on_event("startup")
def startup():
    if is_postgres():
        import psycopg  # fail early with a useful deploy log if dependency/config is wrong
    init_db()

def admin(secret):
    if not ADMIN_SECRET:
        raise HTTPException(503, "ADMIN_SECRET is not configured")
    if not secret or not secrets.compare_digest(secret, ADMIN_SECRET):
        raise HTTPException(401, "Invalid admin secret")

def tid(x):
    x = str(x).strip()
    if not x.isdigit() or len(x) > 24:
        raise HTTPException(400, "telegram_id must be numeric")
    return x

def ensure(c, x):
    if is_postgres():
        execute(c, "INSERT INTO users(telegram_id) VALUES(?) ON CONFLICT(telegram_id) DO NOTHING", (x,))
    else:
        execute(c, "INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (x,))

class Activation(BaseModel):
    key: str = Field(min_length=5, max_length=100)
    telegram_id: str = Field(min_length=1, max_length=24)
class Transfer(BaseModel):
    sender_id: str
    recipient_id: str
    amount: int = Field(gt=0, le=1000000000)
class Generate(BaseModel):
    count: int = Field(default=1, ge=1, le=1000)
    # When supplied, generated keys are pre-bound to this Telegram account.
    telegram_id: str | None = Field(default=None, min_length=1, max_length=24)
class AddCoins(BaseModel):
    telegram_id: str
    amount: int = Field(ge=1, le=1000000000)

@app.get("/")
def root():
    return {"service": "SuperGrammod Backend", "status": "online", "version": "1.2.0", "database": "postgresql" if is_postgres() else "sqlite"}

@app.get("/health")
def health():
    try:
        with db() as c:
            execute(c, "SELECT 1").fetchone()
        return {"ok": True, "database": "postgresql" if is_postgres() else "sqlite"}
    except Exception:
        raise HTTPException(503, "Database unavailable")

@app.post("/activate")
def activate(b: Activation):
    u = tid(b.telegram_id)
    key = b.key.strip()
    with db() as c:
        r = execute(c, "SELECT telegram_id, active FROM licenses WHERE license_key=?", (key,)).fetchone()
        if not r: raise HTTPException(404, "License key not found")
        if not r["active"]: raise HTTPException(403, "License key revoked")
        if r["telegram_id"] and r["telegram_id"] != u: raise HTTPException(409, "Key bound to another account")
        execute(c, "UPDATE licenses SET telegram_id=? WHERE license_key=?", (u, key))
        ensure(c, u)
    return {"ok": True, "telegram_id": u, "message": "License activated"}

@app.post("/admin/generate")
def generate(b: Generate, x_admin_secret: str | None = Header(default=None)):
    admin(x_admin_secret)
    bound_id = tid(b.telegram_id) if b.telegram_id is not None else None
    if bound_id is not None and b.count != 1:
        raise HTTPException(400, "When telegram_id is supplied, count must be 1")
    keys = []
    alphabet = string.ascii_uppercase + string.digits
    with db() as c:
        for _ in range(b.count):
            k = "SGM-" + "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
            # Pre-bind at creation time; another account cannot win a first-activation race.
            execute(c, "INSERT INTO licenses(license_key,telegram_id,created_at) VALUES(?,?,?)", (k, bound_id, int(time.time())))
            keys.append(k)
    result = {"ok": True, "count": len(keys), "keys": keys}
    if bound_id is not None:
        result["bound_telegram_id"] = bound_id
    return result

@app.post("/admin/bind/{license_key}")
def bind_license(license_key: str, telegram_id: str, x_admin_secret: str | None = Header(default=None)):
    """Bind an unused license to one Telegram account; cannot transfer an existing binding."""
    admin(x_admin_secret)
    u = tid(telegram_id)
    key = license_key.strip()
    with db() as c:
        row = execute(c, "SELECT telegram_id, active FROM licenses WHERE license_key=?", (key,)).fetchone()
        if not row:
            raise HTTPException(404, "License key not found")
        if not row["active"]:
            raise HTTPException(403, "License key revoked")
        existing = row["telegram_id"]
        if existing and str(existing) != u:
            raise HTTPException(409, "Key is already bound to another account")
        execute(c, "UPDATE licenses SET telegram_id=? WHERE license_key=?", (u, key))
    return {"ok": True, "license_key": key, "telegram_id": u, "bound": True}

@app.post("/admin/revoke/{license_key}")
def revoke(license_key: str, x_admin_secret: str | None = Header(default=None)):
    admin(x_admin_secret)
    with db() as c:
        r = execute(c, "UPDATE licenses SET active=0 WHERE license_key=?", (license_key,))
        if not r.rowcount: raise HTTPException(404, "License key not found")
    return {"ok": True, "revoked": license_key}

@app.get("/balance/{telegram_id}")
def balance(telegram_id: str):
    u = tid(telegram_id)
    with db() as c:
        r = execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()
    return {"telegram_id": u, "coins": int(r["coins"]) if r else 0}

@app.post("/daily/{telegram_id}")
def daily(telegram_id: str):
    u = tid(telegram_id)
    now = int(time.time())
    with db() as c:
        ensure(c, u)
        r = execute(c, "SELECT last_daily FROM users WHERE telegram_id=?", (u,)).fetchone()
        remaining = 10800 - (now - int(r["last_daily"]))
        if remaining > 0:
            raise HTTPException(429, {"message": "Daily reward is on cooldown", "retry_after_seconds": remaining})
        reward = secrets.randbelow(4951) + 50
        execute(c, "UPDATE users SET coins=coins+?, last_daily=? WHERE telegram_id=?", (reward, now, u))
        total = execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()["coins"]
    return {"ok": True, "telegram_id": u, "reward": reward, "coins": int(total), "next_daily_in_seconds": 10800}

@app.post("/transfer")
def transfer(b: Transfer):
    sender, recipient = tid(b.sender_id), tid(b.recipient_id)
    if sender == recipient: raise HTTPException(400, "Cannot transfer to the same account")
    now = int(time.time())
    with db() as c:
        # Ensure rows exist before updating; database transaction protects concurrent updates.
        ensure(c, sender)
        ensure(c, recipient)
        s = execute(c, "SELECT coins FROM users WHERE telegram_id=?", (sender,)).fetchone()
        if int(s["coins"]) < b.amount: raise HTTPException(400, "Insufficient balance")
        execute(c, "UPDATE users SET coins=coins-? WHERE telegram_id=?", (b.amount, sender))
        execute(c, "UPDATE users SET coins=coins+? WHERE telegram_id=?", (b.amount, recipient))
        execute(c, "INSERT INTO transfers(sender_id,recipient_id,amount,created_at) VALUES(?,?,?,?)", (sender, recipient, b.amount, now))
        new_balance = execute(c, "SELECT coins FROM users WHERE telegram_id=?", (sender,)).fetchone()["coins"]
    return {"ok": True, "sender_id": sender, "recipient_id": recipient, "amount": b.amount, "sender_balance": int(new_balance)}

@app.get("/top")
def top(limit: int = 10):
    """Global leaderboard, sorted by coin balance descending."""
    if limit < 1 or limit > 100:
        raise HTTPException(400, "limit must be between 1 and 100")
    with db() as c:
        rows = execute(
            c,
            "SELECT telegram_id, coins FROM users ORDER BY coins DESC, telegram_id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return {
        "ok": True,
        "count": len(rows),
        "top": [
            {"rank": i, "telegram_id": str(row["telegram_id"]), "coins": int(row["coins"])}
            for i, row in enumerate(rows, start=1)
        ],
    }

@app.get("/profile/{telegram_id}")
def profile(telegram_id: str):
    u = tid(telegram_id)
    with db() as c:
        row = execute(c, "SELECT telegram_id, coins, last_daily FROM users WHERE telegram_id=?", (u,)).fetchone()
        if not row:
            return {"ok": True, "telegram_id": u, "coins": 0, "rank": None, "last_daily": 0}
        ahead = execute(c, "SELECT COUNT(*) AS n FROM users WHERE coins > ?", (int(row["coins"]),)).fetchone()
    return {
        "ok": True,
        "telegram_id": u,
        "coins": int(row["coins"]),
        "rank": int(ahead["n"]) + 1,
        "last_daily": int(row["last_daily"]),
    }

@app.post("/admin/coins/add")
def add_coins(b: AddCoins, x_admin_secret: str | None = Header(default=None)):
    admin(x_admin_secret)
    u = tid(b.telegram_id)
    with db() as c:
        ensure(c, u)
        execute(c, "UPDATE users SET coins=coins+? WHERE telegram_id=?", (b.amount, u))
        total = execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()["coins"]
    return {"ok": True, "telegram_id": u, "added": b.amount, "coins": int(total)}
