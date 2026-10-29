import os, secrets, sqlite3, string, time
from contextlib import contextmanager
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DB_PATH=os.getenv("DB_PATH","supergrammod.db")
ADMIN_SECRET=os.getenv("ADMIN_SECRET","")
app=FastAPI(title="SuperGrammod Backend",version="1.0.0")

@contextmanager
def db():
    c=sqlite3.connect(DB_PATH,timeout=20)
    c.row_factory=sqlite3.Row
    try:
        yield c
        c.commit()
    finally: c.close()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(telegram_id TEXT PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 0 CHECK(coins>=0), last_daily INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS licenses(license_key TEXT PRIMARY KEY, telegram_id TEXT, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY AUTOINCREMENT,sender_id TEXT NOT NULL,recipient_id TEXT NOT NULL,amount INTEGER NOT NULL,created_at INTEGER NOT NULL);
        """)
@app.on_event("startup")
def startup(): init_db()
def admin(secret):
    if not ADMIN_SECRET: raise HTTPException(503,"ADMIN_SECRET is not configured")
    if not secret or not secrets.compare_digest(secret,ADMIN_SECRET): raise HTTPException(401,"Invalid admin secret")
def tid(x):
    x=str(x).strip()
    if not x.isdigit() or len(x)>24: raise HTTPException(400,"telegram_id must be numeric")
    return x
def ensure(c,x): c.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)",(x,))

class Activation(BaseModel):
    key:str=Field(min_length=5,max_length=100)
    telegram_id:str=Field(min_length=1,max_length=24)
class Transfer(BaseModel):
    sender_id:str
    recipient_id:str
    amount:int=Field(gt=0,le=1000000000)
class Generate(BaseModel):
    count:int=Field(default=1,ge=1,le=1000)
class AddCoins(BaseModel):
    telegram_id:str
    amount:int=Field(ge=1,le=1000000000)

@app.get("/")
def root(): return {"service":"SuperGrammod Backend","status":"online","version":"1.0.0"}
@app.get("/health")
def health(): return {"ok":True}
@app.post("/activate")
def activate(b:Activation):
    u=tid(b.telegram_id)
    with db() as c:
        r=c.execute("SELECT telegram_id,active FROM licenses WHERE license_key=?",(b.key.strip(),)).fetchone()
        if not r: raise HTTPException(404,"License key not found")
        if not r["active"]: raise HTTPException(403,"License key revoked")
        if r["telegram_id"] and r["telegram_id"]!=u: raise HTTPException(409,"Key bound to another account")
        c.execute("UPDATE licenses SET telegram_id=? WHERE license_key=?",(u,b.key.strip())); ensure(c,u)
    return {"ok":True,"telegram_id":u,"message":"License activated"}
@app.post("/admin/generate")
def generate(b:Generate,x_admin_secret:str|None=Header(default=None)):
    admin(x_admin_secret); keys=[]; alphabet=string.ascii_uppercase+string.digits
    with db() as c:
        for _ in range(b.count):
            k="SGM-"+"-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
            c.execute("INSERT INTO licenses(license_key,created_at) VALUES(?,?)",(k,int(time.time()))); keys.append(k)
    return {"ok":True,"count":len(keys),"keys":keys}
@app.post("/admin/revoke/{license_key}")
def revoke(license_key:str,x_admin_secret:str|None=Header(default=None)):
    admin(x_admin_secret)
    with db() as c:
        r=c.execute("UPDATE licenses SET active=0 WHERE license_key=?",(license_key,))
        if not r.rowcount: raise HTTPException(404,"License key not found")
    return {"ok":True,"revoked":license_key}
@app.get("/balance/{telegram_id}")
def balance(telegram_id:str):
    u=tid(telegram_id)
    with db() as c:
        ensure(c,u); r=c.execute("SELECT coins FROM users WHERE telegram_id=?",(u,)).fetchone()
    return {"ok":True,"telegram_id":u,"coins":r["coins"]}
@app.post("/daily/{telegram_id}")
def daily(telegram_id:str):
    u=tid(telegram_id); now=int(time.time()); cooldown=10800
    with db() as c:
        ensure(c,u); r=c.execute("SELECT coins,last_daily FROM users WHERE telegram_id=?",(u,)).fetchone()
        left=cooldown-(now-r["last_daily"])
        if left>0: raise HTTPException(429,detail={"message":"Bonus cooldown","seconds_remaining":left})
        reward=secrets.randbelow(4951)+50
        c.execute("UPDATE users SET coins=coins+?,last_daily=? WHERE telegram_id=?",(reward,now,u))
        total=r["coins"]+reward
    return {"ok":True,"reward":reward,"coins":total,"cooldown_seconds":cooldown}
@app.post("/transfer")
def transfer(b:Transfer):
    s=tid(b.sender_id); r=tid(b.recipient_id)
    if s==r: raise HTTPException(400,"Cannot transfer to yourself")
    with db() as c:
        c.execute("BEGIN IMMEDIATE"); ensure(c,s); ensure(c,r)
        bal=c.execute("SELECT coins FROM users WHERE telegram_id=?",(s,)).fetchone()["coins"]
        if bal<b.amount: raise HTTPException(400,"Insufficient coins")
        c.execute("UPDATE users SET coins=coins-? WHERE telegram_id=?",(b.amount,s))
        c.execute("UPDATE users SET coins=coins+? WHERE telegram_id=?",(b.amount,r))
        c.execute("INSERT INTO transfers(sender_id,recipient_id,amount,created_at) VALUES(?,?,?,?)",(s,r,b.amount,int(time.time())))
    return {"ok":True,"sender_id":s,"recipient_id":r,"amount":b.amount,"sender_balance":bal-b.amount}
@app.post("/admin/coins/add")
def add_coins(b:AddCoins,x_admin_secret:str|None=Header(default=None)):
    admin(x_admin_secret); u=tid(b.telegram_id)
    with db() as c:
        ensure(c,u); c.execute("UPDATE users SET coins=coins+? WHERE telegram_id=?",(b.amount,u))
        total=c.execute("SELECT coins FROM users WHERE telegram_id=?",(u,)).fetchone()["coins"]
    return {"ok":True,"telegram_id":u,"coins":total}
