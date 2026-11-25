-- 基盤の改善候補の台帳。研究（research_projects）とは別の系統で、研究の記録は参照するだけで変更しない。
-- 評価計画、実行、判定、操作の記録は追記だけ（UPDATE・DELETE を DB の側で拒否する）。

-- 継続して調べる領域（自己改善型ハーネス、記憶、評価 など）
CREATE TABLE improvement_areas (
  key               text PRIMARY KEY,
  title             text NOT NULL,
  target_problem    text NOT NULL,          -- 既存のどの問題に効くという仮説か
  hypothesis        text NOT NULL,
  comparison        text NOT NULL,          -- 何と比べるか
  resources         text NOT NULL,          -- 必要な資源
  reject_conditions text NOT NULL,          -- 採用しない条件
  status            text NOT NULL DEFAULT 'watching' CHECK (status IN ('watching', 'active', 'paused')),
  notes             text NOT NULL DEFAULT '',
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- 外部の資料（論文、公式リポジトリ、公式文書）。読んだ範囲を必ず区別する
CREATE TABLE improvement_sources (
  id            bigserial PRIMARY KEY,
  area_key      text REFERENCES improvement_areas(key) ON DELETE SET NULL,
  url           text NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('paper', 'repository', 'documentation', 'article')),
  title         text NOT NULL,
  version       text NOT NULL DEFAULT '',   -- arXiv の版、リポジトリのコミットなど
  retrieved_on  date NOT NULL,
  read_level    text NOT NULL CHECK (read_level IN ('listed', 'abstract', 'partial', 'fulltext')),
  locator       text NOT NULL DEFAULT '',   -- 根拠の箇所（節、行など）
  code_commit   text NOT NULL DEFAULT '',
  note          text NOT NULL DEFAULT '',
  UNIQUE (url, version)
);

CREATE TABLE improvement_candidates (
  id                 text PRIMARY KEY,       -- IC-YYYYMMDD-xxxxxx
  parent_id          text REFERENCES improvement_candidates(id),
  area_key           text REFERENCES improvement_areas(key),
  target             text NOT NULL,          -- 変更してよい対象の名前（aar.improve.targets）
  base_commit        text NOT NULL,          -- 対象にした基盤のコミット
  origin_kind        text NOT NULL CHECK (origin_kind IN ('run_record', 'audit_finding', 'paper', 'manual')),
  origin_fingerprint text NOT NULL,          -- 同じ失敗・同じ論文からの乱造を抑えるための指紋
  origin             jsonb NOT NULL DEFAULT '{}',
  problem            text NOT NULL,
  hypothesis         text NOT NULL,
  applies_when       text NOT NULL DEFAULT '',
  side_effects       text NOT NULL DEFAULT '',
  allowed_paths      text[] NOT NULL,
  patch              text,                   -- 候補の差分（生成後）
  patch_sha256       text,
  candidate_commit   text,
  generation         jsonb NOT NULL DEFAULT '{}',   -- モデル、道具、隔離の方法、利用量
  status             text NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'generated', 'invalid', 'evaluated', 'adopt_candidate', 'rejected', 'on_hold')),
  status_reason      text NOT NULL DEFAULT '',
  rejected_under     text NOT NULL DEFAULT '',      -- 不採用になった条件（「常に無効」と一般化しないため）
  created_by         text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX improvement_candidates_origin_idx ON improvement_candidates (target, origin_fingerprint);

-- 評価計画。候補を作る前に固定する。内容のハッシュと、評価器のファイルのハッシュを持つ
CREATE TABLE improvement_eval_plans (
  id                 bigserial PRIMARY KEY,
  target             text NOT NULL,
  plan               jsonb NOT NULL,
  plan_sha256        text NOT NULL UNIQUE,
  evaluator_sha256   text NOT NULL,
  cases_sha256       text NOT NULL,          -- 評価に使う課題（入力と正解）のハッシュ
  created_by         text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE improvement_runs (
  id             bigserial PRIMARY KEY,
  run_key        text NOT NULL UNIQUE,       -- 冪等キー。中断・再開で同じ実行を重ねない
  plan_id        bigint NOT NULL REFERENCES improvement_eval_plans(id),
  candidate_id   text REFERENCES improvement_candidates(id),   -- NULL は現行方式（基準値）
  variant        text NOT NULL CHECK (variant IN ('baseline', 'candidate')),
  split          text NOT NULL CHECK (split IN ('dev', 'select', 'sealed')),
  commit_sha     text NOT NULL,
  status         text NOT NULL CHECK (status IN ('completed', 'failed')),
  failure_kind   text NOT NULL DEFAULT ''    -- code_error / timeout / isolation_unavailable / evaluator_changed / output_invalid
                 CHECK (failure_kind IN ('', 'code_error', 'timeout', 'isolation_unavailable', 'evaluator_changed', 'output_invalid')),
  isolation      text NOT NULL,              -- seatbelt / process
  environment    jsonb NOT NULL DEFAULT '{}',
  metrics        jsonb NOT NULL DEFAULT '{}',
  per_case       jsonb NOT NULL DEFAULT '[]',
  usage          jsonb NOT NULL DEFAULT '{}',   -- 外部への呼び出し、内部の待ち、時間
  raw_path       text NOT NULL DEFAULT '',
  actor          text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE improvement_decisions (
  id            bigserial PRIMARY KEY,
  candidate_id  text NOT NULL REFERENCES improvement_candidates(id),
  plan_id       bigint NOT NULL REFERENCES improvement_eval_plans(id),
  verdict       text NOT NULL CHECK (verdict IN ('adopt_candidate', 'rejected', 'on_hold')),
  checks        jsonb NOT NULL,              -- 基準ごとの結果
  reasons       jsonb NOT NULL,
  decided_by    text NOT NULL,               -- 機械的な判定は policy
  approved_by   text,                        -- 人の承認（基盤への反映は別の操作）
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- 操作の記録。操作主体の種類を必ず持つ（AI の操作を人の操作として記録しない）
CREATE TABLE improvement_events (
  id            bigserial PRIMARY KEY,
  candidate_id  text REFERENCES improvement_candidates(id),
  event         text NOT NULL,
  actor_kind    text NOT NULL CHECK (actor_kind IN ('human', 'claude_code', 'codex', 'scheduler', 'policy')),
  actor_detail  text NOT NULL DEFAULT '',
  run_id        bigint REFERENCES improvement_runs(id),
  detail        jsonb NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX improvement_events_candidate_idx ON improvement_events (candidate_id, id);

CREATE FUNCTION improvement_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% は追記だけ（% は拒否）', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER improvement_eval_plans_append_only BEFORE UPDATE OR DELETE ON improvement_eval_plans
  FOR EACH ROW EXECUTE FUNCTION improvement_append_only();
CREATE TRIGGER improvement_runs_append_only BEFORE UPDATE OR DELETE ON improvement_runs
  FOR EACH ROW EXECUTE FUNCTION improvement_append_only();
CREATE TRIGGER improvement_decisions_append_only BEFORE UPDATE OR DELETE ON improvement_decisions
  FOR EACH ROW EXECUTE FUNCTION improvement_append_only();
CREATE TRIGGER improvement_events_append_only BEFORE UPDATE OR DELETE ON improvement_events
  FOR EACH ROW EXECUTE FUNCTION improvement_append_only();
