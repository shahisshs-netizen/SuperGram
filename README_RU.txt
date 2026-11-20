SUPERGRAM API — ЕДИНЫЙ BACKEND

Включено:
- Neon PostgreSQL: лицензии, NFT, балансы и история игр.
- Лицензии: POST /activate, POST /admin/generate, /admin/generate-for-user,
  /admin/licenses, /admin/revoke, /admin/revoke-user, /admin/user.
- NFT: POST /nft/sync и POST /nft/lookup.
- Игры: POST /game/play (football, basketball, casino; только виртуальные очки).
- Статистика: POST /stats/me, GET /stats/leaderboard.
- Страница статуса: /, проверка БД: /health, документация: /docs.

УСТАНОВКА С НУЛЯ
1. Распакуй ZIP.
2. Создай новый репозиторий GitHub и положи main.py, requirements.txt, render.yaml в корень.
3. Render -> New -> Web Service -> подключи репозиторий.
4. В Environment задай DATABASE_URL (Neon connection string с sslmode=require) и ADMIN_SECRET (случайный длинный секрет).
5. Deploy. Проверь /health и /docs.

АДМИНИСТРИРОВАНИЕ
Все /admin/* требуют заголовок X-Admin-Secret: <ADMIN_SECRET>.
Не клади ADMIN_SECRET в плагин и не публикуй его. Пример генерации:
POST /admin/generate
JSON: {"amount":10,"note":"batch"}

Привязать ключ к аккаунту:
POST /admin/licenses
JSON: {"key":"SGM-...","telegram_id":"123456789"}

Создать ключ сразу для аккаунта:
POST /admin/generate-for-user
JSON: {"telegram_id":"123456789"}

ПРИМЕЧАНИЯ
- Это новый сервер, он не импортирует автоматически старые лицензии, NFT или балансы.
- Плагин должен использовать точные схемы запросов и URL этого сервера. Для NFT отправляется не более 60 элементов за один запрос.
- /nft/sync по умолчанию обновляет переданные элементы; replace_all=true удаляет старый снимок пользователя и должен использоваться только если отправляется полный список.
- Игры используют виртуальные очки, без денежных ставок или выплат.
- Старый серверный код не восстановлен; этот backend реализует указанные API, но другие неизвестные старые endpoints сюда не включены.
