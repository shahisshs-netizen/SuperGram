-- SuperGram Global API: приведение идентификаторов NFT/владельцев к TEXT.
-- Безопасно запускать повторно. Выполняется одной транзакцией: при любой ошибке
-- откатывается целиком и данные остаются как были.
-- Запускать в Neon SQL Editor (или psql) под владельцем таблиц, ПОСЛЕ ветки/бэкапа.

BEGIN;

-- 0. Таблицы, если их ещё нет (существующие не трогает).
CREATE TABLE IF NOT EXISTS licenses (id BIGSERIAL PRIMARY KEY, license_key TEXT UNIQUE NOT NULL, account_id TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), activated_at TIMESTAMPTZ, revoked BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS global_nfts (nft_key TEXT PRIMARY KEY, title TEXT, owner_id TEXT, serial_number TEXT, comment TEXT, nft_date TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS global_nft_items (owner_id TEXT NOT NULL, item_key TEXT NOT NULL, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(owner_id, item_key));
CREATE TABLE IF NOT EXISTS global_bans (account_id TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());

-- 1. Резервные копии (один раз; удалить после проверки).
CREATE TABLE IF NOT EXISTS bak_global_nfts_20261123      AS TABLE global_nfts;
CREATE TABLE IF NOT EXISTS bak_global_nft_items_20261123 AS TABLE global_nft_items;
CREATE TABLE IF NOT EXISTS bak_licenses_20261123         AS TABLE licenses;
CREATE TABLE IF NOT EXISTS bak_global_bans_20261123      AS TABLE global_bans;

-- 2. Числовые колонки идентификаторов -> TEXT с сохранением значений (::text).
--    licenses.id (BIGSERIAL) намеренно не трогается: это внутренний номер строки.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT c.table_name, c.column_name, c.data_type
    FROM information_schema.columns c
    WHERE c.table_schema = current_schema()
      AND c.data_type IN ('bigint', 'integer', 'smallint')
      AND (c.table_name, c.column_name) IN (
        ('licenses', 'account_id'),
        ('global_nfts', 'nft_key'),
        ('global_nfts', 'owner_id'),
        ('global_nfts', 'serial_number'),
        ('global_nft_items', 'owner_id'),
        ('global_nft_items', 'item_key'),
        ('global_bans', 'account_id')
      )
  LOOP
    EXECUTE format('ALTER TABLE %I ALTER COLUMN %I DROP DEFAULT', r.table_name, r.column_name);
    EXECUTE format('ALTER TABLE %I ALTER COLUMN %I TYPE TEXT USING %I::text', r.table_name, r.column_name, r.column_name);
    RAISE NOTICE 'converted %.% from % to text', r.table_name, r.column_name, r.data_type;
  END LOOP;
END $$;

-- 3. Недостающие колонки в уже существующих таблицах (если боевая схема старее кода).
ALTER TABLE global_nft_items ADD COLUMN IF NOT EXISTS owner_id   TEXT;
ALTER TABLE global_nft_items ADD COLUMN IF NOT EXISTS item_key   TEXT;
ALTER TABLE global_nft_items ADD COLUMN IF NOT EXISTS payload    JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE global_nft_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE global_nfts ADD COLUMN IF NOT EXISTS owner_id      TEXT;
ALTER TABLE global_nfts ADD COLUMN IF NOT EXISTS title         TEXT;
ALTER TABLE global_nfts ADD COLUMN IF NOT EXISTS serial_number TEXT;
ALTER TABLE global_nfts ADD COLUMN IF NOT EXISTS comment       TEXT;
ALTER TABLE global_nfts ADD COLUMN IF NOT EXISTS nft_date      TEXT;
ALTER TABLE global_nfts ADD COLUMN IF NOT EXISTS updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- 4. Уникальность, нужная для ON CONFLICT (owner_id, item_key).
--    Если в данных есть дубликаты, команда упадёт и вся транзакция откатится.
CREATE UNIQUE INDEX IF NOT EXISTS global_nft_items_owner_item_uq
  ON global_nft_items (owner_id, item_key);
CREATE INDEX IF NOT EXISTS global_nfts_owner_idx ON global_nfts (owner_id);

-- 5. Контроль: ни одна из колонок ниже не должна остаться не-text.
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = current_schema()
  AND (table_name, column_name) IN (
    ('licenses', 'license_key'), ('licenses', 'account_id'),
    ('global_nfts', 'nft_key'), ('global_nfts', 'owner_id'), ('global_nfts', 'serial_number'),
    ('global_nft_items', 'owner_id'), ('global_nft_items', 'item_key'),
    ('global_bans', 'account_id')
  )
ORDER BY table_name, column_name;

COMMIT;

-- После проверки работы API (через несколько дней) бэкапы можно удалить:
-- DROP TABLE bak_global_nfts_20261123, bak_global_nft_items_20261123, bak_licenses_20261123, bak_global_bans_20261123;
