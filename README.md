# SuperGrammod Backend starter

FastAPI starter for license activation, admin key generation/revocation, coin balances, transfers and `/daily` rewards (50–5000 coins, 3-hour cooldown).

## Render
Build command: `pip install -r requirements.txt`
Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
Set environment variable `ADMIN_SECRET` to a long random secret.

## Endpoints
- `GET /` and `GET /health`
- `POST /activate` JSON `{"key":"SGM-ABCD-EFGH-IJKL","telegram_id":"123456789"}`
- `POST /admin/generate` header `X-Admin-Secret`, JSON `{"count":100}`
- `POST /admin/revoke/KEY` header `X-Admin-Secret`
- `GET /balance/123456789`
- `POST /daily/123456789`
- `POST /transfer` JSON `{"sender_id":"123456789","recipient_id":"987654321","amount":50}`
- `POST /admin/coins/add` header `X-Admin-Secret`, JSON `{"telegram_id":"123456789","amount":100}`

IMPORTANT: this starter uses SQLite for initial testing. Render free services have ephemeral disk, so do not use it for real balances/licenses until a persistent database is connected. Also add client authentication before connecting it to a public plugin.
