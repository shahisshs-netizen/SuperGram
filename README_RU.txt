SuperGrammod — готовый модуль глобального NFT-реестра Neon

СОДЕРЖИМОЕ
- nft_server.py — модуль FastAPI для подключения к существующему серверу.
- main_integration.py.txt — строки, которые должны присутствовать в существующем main.py.
- requirements_add.txt — зависимости, которые нужно ДОБАВИТЬ в существующий requirements.txt.

ВАЖНО
Это готовый модуль NFT, но не полная замена всего backend-проекта: без исходного main.py нельзя безопасно пересобрать весь сервер и сохранить остальные API.

КАК УСТАНОВИТЬ
1. В репозитории GitHub, который подключён к Render, замени старый nft_server.py файлом nft_server.py из архива.
2. Открой main.py и проверь, что внутри приложения FastAPI есть:
       from nft_server import router as nft_router
       app.include_router(nft_router)
   Если этих строк нет — добавь их после создания app = FastAPI(...).
3. Добавь отсутствующие зависимости из requirements_add.txt в существующий requirements.txt. Не стирай другие зависимости проекта.
4. В Render → нужный Web Service → Environment задай:
       DATABASE_URL = строка подключения Neon с SSL (обычно sslmode=require)
       NFT_VERIFY_URL = https://supergram.onrender.com/activate
   Используй этот NFT_VERIFY_URL только если действующий эндпоинт активации действительно находится по этому адресу и принимает POST JSON {"key":"...", "telegram_id":"..."}, отвечая {"ok": true} для действительной лицензии. Иначе укажи фактический адрес/формат своего endpoint.
5. Сохрани изменения в GitHub и дождись успешного деплоя Render.

API
POST /nft/sync
POST /nft/lookup

Формат /nft/sync:
{
  "telegram_id": "123456789",
  "key": "SGM-...",
  "items": [
    {"dialog_id": 123456789, "key": "gift-1", "gift": "..."}
  ]
}
По умолчанию синхронизация добавляет/обновляет переданные записи и не удаляет остальные. Для полного снимка можно передать "replace_all": true, но только когда клиент отправляет полный список NFT аккаунта.

Формат /nft/lookup:
{
  "telegram_id": "123456789",
  "key": "SGM-...",
  "targets": ["123456789"]
}

ПРОВЕРКА
- Убедись, что Render завершил сборку и запуск без ошибок.
- Проверь POST /nft/sync и POST /nft/lookup через свой API-клиент.
- Этот модуль не меняет код плагина SuperGrammod. Для реальной синхронизации клиент плагина должен отправлять запросы на эти endpoints.
- Не публикуй DATABASE_URL или другие секреты.
