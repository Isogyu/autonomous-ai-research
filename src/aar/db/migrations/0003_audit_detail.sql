-- 監査の詳細（内訳の点数、保留点、判定の根拠、結果の検証、実行ログの場所など）
ALTER TABLE audit_runs ADD COLUMN detail jsonb NOT NULL DEFAULT '{}';
-- 論文の本文を取得した記録（本文そのものは DB にも Git にも入れず、var/papers に置く）
ALTER TABLE papers ADD COLUMN fulltext_url text;
ALTER TABLE papers ADD COLUMN fulltext_sha256 text;
ALTER TABLE papers ADD COLUMN fulltext_retrieved_at timestamptz;
ALTER TABLE papers ADD COLUMN fulltext_chars integer;
