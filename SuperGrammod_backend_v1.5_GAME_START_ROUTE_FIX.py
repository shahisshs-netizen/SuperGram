import os, secrets, string, time
from contextlib import contextmanager
from urllib.parse import urlparse
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("DB_PATH", "supergrammod.db")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
app = FastAPI(title="SuperGrammod Backend", version="1.4.0")

# Active multiplier rounds are server-authoritative. Restarting the service cancels open rounds.
GAME_ROUNDS = {}
ROULETTE_BETS = {}

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
class AddCoins(BaseModel):
    telegram_id: str
    amount: int = Field(ge=1, le=1000000000)

class GameBet(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=24)
    amount: int = Field(gt=0, le=150000000000)
    game: str = "mines"

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
    keys = []
    alphabet = string.ascii_uppercase + string.digits
    with db() as c:
        for _ in range(b.count):
            k = "SGM-" + "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
            execute(c, "INSERT INTO licenses(license_key,created_at) VALUES(?,?)", (k, int(time.time())))
            keys.append(k)
    return {"ok": True, "count": len(keys), "keys": keys}

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

@app.post("/game/{game}")
def play_game(game: str, b: GameBet):
    """Play a virtual-coin game against the authoritative shared wallet."""
    game = game.strip().lower()
    aliases = {"football": "fut", "basket": "bas", "basketball": "bas", "casik": "casino"}
    game = aliases.get(game, game)
    # FastAPI matches routes in declaration order: /game/{game} was declared
    # before /game/start, so "start" arrived here as the game name.
    # Dispatch it explicitly to the multiplier-round handler.
    if game == "start":
        return start_multiplier_game(GameStart(
            telegram_id=b.telegram_id,
            amount=b.amount,
            game=b.game
        ))
    if game not in ("fut", "bas", "casino", "tower", "mines", "roc"):
        raise HTTPException(404, "Unsupported game; use fut, bas, casino, tower, mines, or roc")
    u = tid(b.telegram_id)
    now = int(time.time())
    # Outcomes are generated on the server; clients cannot choose the payout.
    roll = secrets.randbelow(10000) / 10000.0
    if game == "fut":
        payout = b.amount * 2 if roll < 0.50 else 0
        outcome = "goal" if payout else "miss"
    elif game == "bas":
        if roll < 0.45:
            payout, outcome = b.amount * 2, "score"
        elif roll < 0.60:
            payout, outcome = b.amount, "rim"
        else:
            payout, outcome = 0, "miss"
    elif game == "casino":
        if roll < 0.10:
            payout, outcome = b.amount * 4, "jackpot"
        elif roll < 0.35:
            payout, outcome = b.amount * 2, "win"
        else:
            payout, outcome = 0, "loss"
    elif game == "tower":
        if roll < 0.35:
            payout, outcome = b.amount * 3, "climb"
        else:
            payout, outcome = 0, "fall"
    elif game == "mines":
        if roll < 0.45:
            payout, outcome = b.amount * 2, "safe"
        else:
            payout, outcome = 0, "mine"
    else:  # roc: server-side rock-paper-scissors style single-round wager
        if roll < 1 / 3:
            payout, outcome = b.amount * 2, "rock_win"
        elif roll < 2 / 3:
            payout, outcome = b.amount, "rock_draw"
        else:
            payout, outcome = 0, "rock_loss"
    with db() as c:
        ensure(c, u)
        row = execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()
        balance = int(row["coins"])
        if balance < b.amount:
            raise HTTPException(400, {"message": "Insufficient balance", "coins": balance})
        # Conditional update prevents concurrent bets from spending the same coins twice.
        changed = execute(c, "UPDATE users SET coins=coins-?+? WHERE telegram_id=? AND coins>=?",
                          (b.amount, payout, u, b.amount))
        if not changed.rowcount:
            latest = execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()
            raise HTTPException(400, {"message": "Insufficient balance", "coins": int(latest["coins"])})
        balance = int(execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()["coins"])
    return {"ok": True, "game": game, "telegram_id": u, "bet": b.amount,
            "payout": payout, "net": payout-b.amount, "outcome": outcome,
            "coins": balance, "played_at": now}



class GameStart(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=24)
    amount: int = Field(gt=0, le=150000000000)
    game: str = Field(default="mines")

class RouletteBet(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=24)
    amount: int = Field(gt=0, le=150000000000)
    choice: str = Field(min_length=1, max_length=20)

@app.post("/game/start")
def start_multiplier_game(b: GameStart):
    u = tid(b.telegram_id)
    game = b.game.strip().lower()
    if game not in ("mines", "tower", "casino", "roc"):
        raise HTTPException(400, "Unsupported multiplier game")
    now = int(time.time())
    with db() as c:
        ensure(c, u)
        row = execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()
        if int(row["coins"]) < b.amount:
            raise HTTPException(400, {"message": "Insufficient balance", "coins": int(row["coins"])})
        changed = execute(c, "UPDATE users SET coins=coins-? WHERE telegram_id=? AND coins>=?", (b.amount, u, b.amount))
        if not changed.rowcount:
            raise HTTPException(400, "Insufficient balance")
        balance = int(execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()["coins"])
    GAME_ROUNDS[u] = {"game": game, "bet": b.amount, "multiplier": 1.0, "steps": 0, "created": now}
    return {"ok": True, "game": game, "bet": b.amount, "multiplier": 1.0, "coins": balance, "message": "Раунд начат: отправь 1, чтобы продолжить, или 2, чтобы забрать выплату."}

@app.post("/game/step/{game}")
def step_multiplier_game(game: str, telegram_id: str):
    u = tid(telegram_id)
    game = game.strip().lower()
    r = GAME_ROUNDS.get(u)
    if not r or r["game"] != game:
        raise HTTPException(400, "Нет активного раунда. Начни новый: /%s СТАВКА" % game)
    # Each safe step raises the multiplier. A failed step loses the already placed bet.
    roll = secrets.randbelow(10000) / 10000.0
    fail_chance = min(0.42, 0.12 + r["steps"] * 0.035)
    if roll < fail_chance:
        GAME_ROUNDS.pop(u, None)
        with db() as c:
            balance = int(execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()["coins"])
        return {"ok": True, "game": game, "outcome": "loss", "bet": r["bet"], "multiplier": r["multiplier"], "payout": 0, "net": -r["bet"], "coins": balance}
    r["steps"] += 1
    r["multiplier"] = round(r["multiplier"] + (0.5 if game == "mines" else 0.65), 2)
    return {"ok": True, "game": game, "outcome": "safe", "bet": r["bet"], "steps": r["steps"], "multiplier": r["multiplier"], "cashout": int(r["bet"] * r["multiplier"])}

@app.post("/game/cashout/{game}")
def cashout_multiplier_game(game: str, telegram_id: str):
    u = tid(telegram_id)
    game = game.strip().lower()
    r = GAME_ROUNDS.get(u)
    if not r or r["game"] != game:
        raise HTTPException(400, "Нет активного раунда")
    if r["steps"] < 1:
        raise HTTPException(400, "Сначала сделай хотя бы один шаг")
    payout = int(r["bet"] * r["multiplier"])
    with db() as c:
        ensure(c, u)
        execute(c, "UPDATE users SET coins=coins+? WHERE telegram_id=?", (payout, u))
        balance = int(execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()["coins"])
    GAME_ROUNDS.pop(u, None)
    return {"ok": True, "game": game, "outcome": "cashout", "bet": r["bet"], "multiplier": r["multiplier"], "payout": payout, "net": payout-r["bet"], "coins": balance}

@app.post("/roulette")
def roulette(b: RouletteBet):
    u = tid(b.telegram_id)
    choice = b.choice.strip().lower()
    red = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    if choice not in ("red", "black") and not (choice.isdigit() and 0 <= int(choice) <= 36):
        raise HTTPException(400, "Выбери red, black или число 0–36")
    with db() as c:
        ensure(c, u)
        row = execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()
        if int(row["coins"]) < b.amount:
            raise HTTPException(400, {"message": "Insufficient balance", "coins": int(row["coins"])})
        execute(c, "UPDATE users SET coins=coins-? WHERE telegram_id=?", (b.amount, u))
        number = secrets.randbelow(37)
        won = (choice == "red" and number in red) or (choice == "black" and number != 0 and number not in red) or (choice.isdigit() and int(choice) == number)
        # Requested multiplier: colour x2, exact number x5; multiplier includes returned stake.
        mult = 2 if choice in ("red", "black") else 5
        payout = b.amount * mult if won else 0
        if payout:
            execute(c, "UPDATE users SET coins=coins+? WHERE telegram_id=?", (payout, u))
        balance = int(execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()["coins"])
    colour = "зелёное" if number == 0 else ("красное" if number in red else "чёрное")
    return {"ok": True, "game":"roulette", "choice":choice, "number":number, "color":colour, "won":bool(won), "multiplier":mult if won else 0, "bet":b.amount, "payout":payout, "net":payout-b.amount, "coins":balance}

@app.post("/admin/coins/add")
def add_coins(b: AddCoins, x_admin_secret: str | None = Header(default=None)):
    admin(x_admin_secret)
    u = tid(b.telegram_id)
    with db() as c:
        ensure(c, u)
        execute(c, "UPDATE users SET coins=coins+? WHERE telegram_id=?", (b.amount, u))
        total = execute(c, "SELECT coins FROM users WHERE telegram_id=?", (u,)).fetchone()["coins"]
    return {"ok": True, "telegram_id": u, "added": b.amount, "coins": int(total)}
