# SuperGrammod Backend

FastAPI backend for license activation, admin license generation/revocation, coin balances, transfers, and `/daily` rewards.

## Render
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Environment variables:
  - `ADMIN_SECRET`: long random secret
  - `DATABASE_URL`: Neon PostgreSQL connection string (recommended)
- If `DATABASE_URL` is absent, the app falls back to local SQLite for testing only. Render's local filesystem is not durable for this purpose.

## Endpoints
- `GET /`, `GET /health`
- `POST /activate` JSON `{"key":"SGM-ABCD-EFGH-IJKL","telegram_id":"123456789"}`
- `POST /admin/generate` header `X-Admin-Secret`, JSON `{"count":100}`
- `POST /admin/revoke/KEY` header `X-Admin-Secret`
- `GET /balance/123456789`
- `POST /daily/123456789` (50–5000 coins, 3-hour cooldown)
- `POST /transfer` JSON `{"sender_id":"123456789","recipient_id":"987654321","amount":50}`
- `POST /admin/coins/add` header `X-Admin-Secret`, JSON `{"telegram_id":"123456789","amount":100}`

Important: before connecting a public Telegram plugin, implement client authentication and test the API. Do not expose `ADMIN_SECRET` in the plugin.


## Global leaderboard

- `GET /top` — global ranking by coins (default 10 players).
- `GET /top?limit=50` — show up to 100 players.
- `GET /profile/{telegram_id}` — balance and current global rank.

The leaderboard contains accounts that exist in the shared server database. Set `DATABASE_URL` on Render to a persistent PostgreSQL database (for example Neon) so rankings and balances are shared and survive redeploys. Keep `ADMIN_SECRET` private and set it in Render Environment, not in source files.
