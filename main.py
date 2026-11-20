"""SuperGram unified backend: licenses, NFT registry, virtual games, stats, admin API.
Deploy on Render with Neon PostgreSQL. Never put ADMIN_SECRET in the client plugin.
"""
from __future__ import annotations
import os, time, secrets, re
from typing import Optional, Literal
from fastapi import FastAPI, Header, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

app = FastAPI(title="SuperGram API", version="2.0.0", description="Unified SuperGram licenses, NFTs, games and stats API")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
MAX_NFT_ITEMS = 60
MAX_NFT_TARGETS = 40
MAX_GIFT_CHARS = 60000
MAX_GAME_BET = 10000
START_BALANCE = 50


def db():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL_NOT_CONFIGURED")
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=10, sslmode="require")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="DATABASE_UNAVAILABLE") from exc


def init_db():
    con = db()
    try:
        with con, con.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS licenses (
                license_key TEXT PRIMARY KEY, telegram_id BIGINT, active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
                bound_at DOUBLE PRECISION, note TEXT NOT NULL DEFAULT ''
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS nft_items (
                publisher_id BIGINT NOT NULL, dialog_id BIGINT NOT NULL, item_key TEXT NOT NULL,
                gift TEXT NOT NULL, from_id BIGINT NOT NULL DEFAULT 0, anonymous BOOLEAN NOT NULL DEFAULT FALSE,
                message TEXT NOT NULL DEFAULT '', pin BOOLEAN NOT NULL DEFAULT FALSE, title TEXT NOT NULL DEFAULT '',
                slug TEXT NOT NULL DEFAULT '', ts DOUBLE PRECISION NOT NULL DEFAULT 0,
                PRIMARY KEY(publisher_id, dialog_id, item_key))""")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_nft_dialog_ts ON nft_items(dialog_id, ts DESC)")
            cur.execute("""CREATE TABLE IF NOT EXISTS players (
                telegram_id BIGINT PRIMARY KEY, balance BIGINT NOT NULL DEFAULT 50,
                total_won BIGINT NOT NULL DEFAULT 0, total_lost BIGINT NOT NULL DEFAULT 0,
                games_played BIGINT NOT NULL DEFAULT 0, updated_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS game_history (
                id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL, game TEXT NOT NULL,
                bet BIGINT NOT NULL, delta BIGINT NOT NULL, result TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()))""")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_game_history_user ON game_history(telegram_id, created_at DESC)")
    finally:
        con.close()

@app.on_event("startup")
def startup():
    init_db()

@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>SuperGram API</title><style>body{font:16px system-ui;max-width:760px;margin:40px auto;padding:0 18px;background:#111318;color:#eee}a{color:#8bb7ff}.card{padding:18px;border:1px solid #383b44;border-radius:14px;margin:14px 0}code{color:#b7e3c0}</style></head><body><h1>SuperGram API</h1><p>Единый backend: NFT, лицензии, виртуальные игры и статистика.</p><div class='card'><b>Состояние:</b> API запущен.<p><a href='/health'>Проверка сервера</a> · <a href='/docs'>API документация</a></p></div><p>Административные методы требуют заголовок <code>X-Admin-Secret</code>. Секрет нельзя встраивать в плагин.</p></body></html>"""

@app.get("/health")
def health():
    con = db()
    try:
        with con.cursor() as cur: cur.execute("SELECT 1")
        return {"ok": True, "database": "connected", "service": "SuperGram API"}
    finally: con.close()

class LicenseBody(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    telegram_id: str = Field(min_length=1, max_length=24)

class GenerateBody(BaseModel):
    amount: int = Field(default=1, ge=1, le=100)
    telegram_id: Optional[str] = Field(default=None, max_length=24)
    note: str = Field(default="", max_length=200)

class UserBody(BaseModel):
    telegram_id: str = Field(min_length=1, max_length=24)

class Item(BaseModel):
    dialog_id: int
    key: str = Field(min_length=1, max_length=80)
    gift: str = Field(min_length=1, max_length=MAX_GIFT_CHARS)
    from_id: int = 0
    anonymous: bool = False
    message: str = Field(default="", max_length=400)
    pin: bool = False
    title: str = Field(default="", max_length=120)
    slug: str = Field(default="", max_length=120)
    ts: float = 0

class SyncBody(LicenseBody):
    items: list[Item] = Field(default_factory=list)
    replace_all: bool = False

class LookupBody(LicenseBody):
    targets: list[str] = Field(default_factory=list)

class GameBody(LicenseBody):
    game: Literal["football", "basketball", "casino"]
    bet: int = Field(default=1, ge=1, le=MAX_GAME_BET)
    choice: Optional[str] = Field(default=None, max_length=20)


def parse_id(raw: str) -> int:
    try: val = int(raw)
    except (TypeError, ValueError): raise HTTPException(status_code=400, detail="BAD_ID")
    if val <= 0: raise HTTPException(status_code=400, detail="BAD_ID")
    return val

def norm_key(key: str) -> str:
    return key.strip().upper()

def require_admin(x_admin_secret: Optional[str] = Header(default=None)):
    if not ADMIN_SECRET or not x_admin_secret or not secrets.compare_digest(x_admin_secret, ADMIN_SECRET):
        raise HTTPException(status_code=403, detail="ADMIN_AUTH_FAILED")

def license_ok(key: str, uid: int) -> bool:
    con = db()
    try:
        with con.cursor() as cur:
            cur.execute("SELECT 1 FROM licenses WHERE license_key=%s AND telegram_id::text=%s AND active=TRUE", (norm_key(key), str(uid)))
            return cur.fetchone() is not None
    finally: con.close()

def require_license(key: str, uid: int):
    if not license_ok(key, uid): raise HTTPException(status_code=403, detail="LICENSE_INVALID_OR_NOT_BOUND_TO_ACCOUNT")

def new_key() -> str:
    return "SGM-" + "-".join(secrets.token_hex(4).upper() for _ in range(3))

@app.post("/activate")
def activate(body: LicenseBody):
    uid = parse_id(body.telegram_id)
    return {"ok": license_ok(body.key, uid)}

@app.post("/admin/generate")
def admin_generate(body: GenerateBody, _: None = Depends(require_admin)):
    uid = parse_id(body.telegram_id) if body.telegram_id else None
    keys=[]
    con=db()
    try:
        with con, con.cursor() as cur:
            for _i in range(body.amount):
                key=new_key()
                cur.execute("INSERT INTO licenses(license_key,telegram_id,active,note,bound_at) VALUES(%s,%s,TRUE,%s,%s)", (key, str(uid) if uid is not None else None, body.note, time.time() if uid else None))
                keys.append(key)
    finally: con.close()
    return {"ok":True,"count":len(keys),"keys":keys}

@app.post("/admin/generate-for-user")
def admin_generate_for_user(body: UserBody, _: None = Depends(require_admin)):
    uid=parse_id(body.telegram_id); key=new_key(); con=db()
    try:
        with con, con.cursor() as cur:
            cur.execute("INSERT INTO licenses(license_key,telegram_id,active,bound_at) VALUES(%s,%s,TRUE,%s)",(key,str(uid),time.time()))
    finally: con.close()
    return {"ok":True,"key":key,"telegram_id":str(uid)}

@app.post("/admin/licenses")
def admin_set_license(body: LicenseBody, active: bool = True, _: None = Depends(require_admin)):
    uid=parse_id(body.telegram_id); key=norm_key(body.key); con=db()
    try:
        with con, con.cursor() as cur:
            cur.execute("INSERT INTO licenses(license_key,telegram_id,active,bound_at) VALUES(%s,%s,%s,%s) ON CONFLICT(license_key) DO UPDATE SET telegram_id=EXCLUDED.telegram_id,active=EXCLUDED.active,bound_at=EXCLUDED.bound_at",(key,str(uid),active,time.time()))
    finally: con.close()
    return {"ok":True,"key":key,"telegram_id":str(uid),"active":active}

@app.post("/admin/revoke")
def admin_revoke(body: LicenseBody, _: None = Depends(require_admin)):
    con=db()
    try:
        with con, con.cursor() as cur: cur.execute("UPDATE licenses SET active=FALSE WHERE license_key=%s",(norm_key(body.key),)); count=cur.rowcount
    finally: con.close()
    return {"ok":True,"revoked":count>0}

@app.post("/admin/revoke-user")
def admin_revoke_user(body: UserBody, _: None = Depends(require_admin)):
    uid=parse_id(body.telegram_id); con=db()
    try:
        with con, con.cursor() as cur: cur.execute("UPDATE licenses SET active=FALSE WHERE telegram_id::text=%s",(str(uid),)); count=cur.rowcount
    finally: con.close()
    return {"ok":True,"revoked_count":count,"telegram_id":str(uid)}

@app.post("/admin/user")
def admin_user(body: UserBody, _: None = Depends(require_admin)):
    uid=parse_id(body.telegram_id); con=db()
    try:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT license_key,telegram_id,active,created_at,note FROM licenses WHERE telegram_id::text=%s ORDER BY created_at DESC",(str(uid),))
            licenses=[{**r,"telegram_id":str(r["telegram_id"])} for r in cur.fetchall()]
            cur.execute("SELECT balance,total_won,total_lost,games_played FROM players WHERE telegram_id=%s",(uid,)); player=cur.fetchone()
    finally: con.close()
    return {"ok":True,"telegram_id":str(uid),"licenses":licenses,"player":player}

@app.post("/nft/sync")
def nft_sync(body: SyncBody):
    uid=parse_id(body.telegram_id); require_license(body.key,uid)
    if len(body.items)>MAX_NFT_ITEMS: raise HTTPException(status_code=413,detail=f"MAX_ITEMS_{MAX_NFT_ITEMS}")
    rows=[]; seen=set()
    for it in body.items:
        if it.dialog_id<=0: continue
        ident=(it.dialog_id,it.key)
        if ident in seen: continue
        seen.add(ident); rows.append((uid,it.dialog_id,it.key,it.gift,it.from_id,int(it.anonymous),it.message,int(it.pin),it.title,it.slug,float(it.ts or time.time())))
    con=db()
    try:
        with con, con.cursor() as cur:
            if body.replace_all: cur.execute("DELETE FROM nft_items WHERE publisher_id=%s",(uid,))
            if rows:
                execute_values(cur,"""INSERT INTO nft_items(publisher_id,dialog_id,item_key,gift,from_id,anonymous,message,pin,title,slug,ts) VALUES %s
                ON CONFLICT(publisher_id,dialog_id,item_key) DO UPDATE SET gift=EXCLUDED.gift,from_id=EXCLUDED.from_id,anonymous=EXCLUDED.anonymous,message=EXCLUDED.message,pin=EXCLUDED.pin,title=EXCLUDED.title,slug=EXCLUDED.slug,ts=EXCLUDED.ts""",rows)
    finally: con.close()
    return {"ok":True,"stored":len(rows),"replace_all":body.replace_all}

@app.post("/nft/lookup")
def nft_lookup(body: LookupBody):
    uid=parse_id(body.telegram_id); require_license(body.key,uid); targets=[]
    for raw in body.targets[:MAX_NFT_TARGETS]:
        try:
            v=int(raw)
            if v>0 and v not in targets: targets.append(v)
        except (ValueError,TypeError): pass
    users={str(t):[] for t in targets}
    if targets:
        con=db()
        try:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT publisher_id,dialog_id,item_key,gift,from_id,anonymous,message,pin,title,slug,ts FROM nft_items WHERE dialog_id=ANY(%s) ORDER BY ts DESC LIMIT 2000",(targets,))
                for r in cur.fetchall(): users[str(r['dialog_id'])].append({"publisher_id":str(r['publisher_id']),"key":r['item_key'],"gift":r['gift'],"from_id":str(r['from_id']),"anonymous":bool(r['anonymous']),"message":r['message'],"pin":bool(r['pin']),"title":r['title'],"slug":r['slug'],"ts":r['ts']})
        finally: con.close()
    return {"ok":True,"users":users}

@app.post("/game/play")
def game_play(body: GameBody):
    uid=parse_id(body.telegram_id); require_license(body.key,uid); bet=body.bet
    con=db()
    try:
        with con, con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO players(telegram_id,balance) VALUES(%s,%s) ON CONFLICT(telegram_id) DO NOTHING",(uid,START_BALANCE))
            cur.execute("SELECT balance FROM players WHERE telegram_id=%s FOR UPDATE",(uid,)); player=cur.fetchone(); balance=int(player['balance'])
            if bet>balance: raise HTTPException(status_code=400,detail="INSUFFICIENT_BALANCE")
            # Virtual-only game outcome; no real-money payments or prizes.
            roll=secrets.randbelow(100)
            if body.game=="casino": win=roll<42; delta=bet if win else -bet
            elif body.game=="football": win=roll<45; delta=bet if win else -bet
            else: win=roll<46; delta=bet if win else -bet
            new_balance=balance+delta
            cur.execute("UPDATE players SET balance=%s,total_won=total_won+%s,total_lost=total_lost+%s,games_played=games_played+1,updated_at=%s WHERE telegram_id=%s",(new_balance,max(delta,0),max(-delta,0),time.time(),uid))
            cur.execute("INSERT INTO game_history(telegram_id,game,bet,delta,result) VALUES(%s,%s,%s,%s,%s)",(uid,body.game,bet,delta,"win" if win else "loss"))
    finally: con.close()
    return {"ok":True,"game":body.game,"result":"win" if win else "loss","delta":delta,"balance":new_balance}

@app.post("/stats/me")
def stats_me(body: LicenseBody):
    uid=parse_id(body.telegram_id); require_license(body.key,uid); con=db()
    try:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT telegram_id,balance,total_won,total_lost,games_played FROM players WHERE telegram_id=%s",(uid,)); r=cur.fetchone()
            if not r: return {"ok":True,"telegram_id":str(uid),"balance":START_BALANCE,"total_won":0,"total_lost":0,"games_played":0}
            r['telegram_id']=str(r['telegram_id']); return {"ok":True,**r}
    finally: con.close()

@app.get("/stats/leaderboard")
def leaderboard(limit: int = 10):
    limit=max(1,min(limit,50)); con=db()
    try:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT telegram_id,balance,total_won,games_played FROM players ORDER BY balance DESC,total_won DESC LIMIT %s",(limit,))
            return {"ok":True,"leaderboard":[{**r,"telegram_id":str(r['telegram_id'])} for r in cur.fetchall()]}
    finally: con.close()

