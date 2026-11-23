-- 研究記憶の初期スキーマ（PostgreSQL 17 + pgvector）
-- 方針:
--   * 事実（外部資料の記載）と AI の推測を origin 列で必ず区別する
--   * 外部から得た知識には出典（source / url / retrieved_at / version）を残す
--   * 副作用のある処理は idempotency_key の一意制約で重複を防ぐ
--   * 実行が本物かモックかを execution_mode で記録し、成功を混同しない

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 知識の出どころ
CREATE TYPE knowledge_origin AS ENUM (
  'external_fact',   -- 外部資料に記載された事実
  'ai_inference',    -- AI が生成した推測・要約・評価
  'measured',        -- このシステムの実験で測った値
  'user'             -- 利用者の入力
);
CREATE TYPE verification_status AS ENUM ('unverified', 'verified', 'contradicted', 'not_tested');
CREATE TYPE execution_mode AS ENUM ('real', 'mock');

-- ---------------------------------------------------------------- 論文

CREATE TABLE papers (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title              text NOT NULL,
  title_norm         text NOT NULL,              -- 重複判定用に正規化した題名
  abstract           text,
  doi                text UNIQUE,                -- 小文字に正規化
  arxiv_id           text UNIQUE,                -- バージョンを除いた ID（例 2609.02095）
  arxiv_version      integer,                    -- 取得済みの最新バージョン
  openalex_id        text UNIQUE,
  s2_id              text UNIQUE,
  published_at       date,
  source_updated_at  timestamptz,                -- 情報源での更新日時
  peer_review_status text NOT NULL DEFAULT 'unknown'
                     CHECK (peer_review_status IN ('preprint', 'peer_reviewed', 'unknown')),
  venue              text,
  categories         text[] NOT NULL DEFAULT '{}',
  url                text,
  pdf_url            text,
  code_url           text,
  license            text,
  citation_count     integer,
  fulltext_status    text NOT NULL DEFAULT 'abstract_only'
                     CHECK (fulltext_status IN ('abstract_only', 'fulltext_read', 'unavailable')),
  first_author_norm  text,
  first_seen_at      timestamptz NOT NULL DEFAULT now(),
  last_seen_at       timestamptz NOT NULL DEFAULT now(),
  tsv                tsvector GENERATED ALWAYS AS (
                       setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
                       setweight(to_tsvector('simple', coalesce(abstract, '')), 'B')
                     ) STORED
);
CREATE INDEX papers_tsv_idx ON papers USING gin (tsv);
CREATE INDEX papers_title_trgm_idx ON papers USING gin (title_norm gin_trgm_ops);
CREATE INDEX papers_published_idx ON papers (published_at DESC);
CREATE INDEX papers_categories_idx ON papers USING gin (categories);

-- 情報源ごとの取得記録（出典と取得日時、原データ）
CREATE TABLE paper_sources (
  id            bigserial PRIMARY KEY,
  paper_id      uuid NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  source        text NOT NULL,                   -- arxiv / openalex / semantic_scholar
  source_id     text NOT NULL,
  url           text,
  retrieved_at  timestamptz NOT NULL DEFAULT now(),
  raw           jsonb NOT NULL DEFAULT '{}',
  UNIQUE (source, source_id)
);
CREATE INDEX paper_sources_paper_idx ON paper_sources (paper_id);

-- 論文のバージョン履歴
CREATE TABLE paper_versions (
  id                bigserial PRIMARY KEY,
  paper_id          uuid NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  version           integer NOT NULL,
  source_updated_at timestamptz,
  retrieved_at      timestamptz NOT NULL DEFAULT now(),
  content_sha256    text NOT NULL,               -- 題名＋要旨のハッシュ
  UNIQUE (paper_id, version)
);

CREATE TABLE authors (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  name_norm   text NOT NULL,
  openalex_id text UNIQUE,
  s2_id       text UNIQUE,
  orcid       text UNIQUE
);
CREATE INDEX authors_name_norm_idx ON authors (name_norm);

CREATE TABLE paper_authors (
  paper_id  uuid NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  author_id uuid NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
  position  integer NOT NULL,
  PRIMARY KEY (paper_id, author_id)
);

-- 引用関係（引用先が未収集でも外部 ID で残す）
CREATE TABLE citations (
  citing_paper_id uuid NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  cited_paper_id  uuid REFERENCES papers(id) ON DELETE CASCADE,
  cited_external_id text NOT NULL,               -- 例 arxiv:2401.00001 / doi:10.x/y
  source          text NOT NULL,
  retrieved_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (citing_paper_id, cited_external_id)
);
CREATE INDEX citations_cited_idx ON citations (cited_paper_id);

-- 価値評価（評価根拠と重みを保存。重みを変えたら再評価できる）
CREATE TABLE paper_scores (
  id             bigserial PRIMARY KEY,
  paper_id       uuid NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  rubric_version text NOT NULL,
  weights_sha256 text NOT NULL,
  criteria       jsonb NOT NULL,                 -- {criterion: {score, rationale}}
  total          numeric(6,3) NOT NULL,
  scorer         text NOT NULL,                  -- heuristic / claude:<model> など
  origin         knowledge_origin NOT NULL DEFAULT 'ai_inference',
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (paper_id, rubric_version, weights_sha256, scorer)
);
CREATE INDEX paper_scores_total_idx ON paper_scores (total DESC);

CREATE TABLE ingest_runs (
  id             bigserial PRIMARY KEY,
  idempotency_key text UNIQUE NOT NULL,          -- 例 ingest:2026-11-23
  started_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  fetched        integer NOT NULL DEFAULT 0,
  inserted       integer NOT NULL DEFAULT 0,
  updated        integer NOT NULL DEFAULT 0,
  merged         integer NOT NULL DEFAULT 0,
  errors         jsonb NOT NULL DEFAULT '[]'
);

-- ---------------------------------------------------------------- 企画と研究

CREATE TABLE research_proposals (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_key     text NOT NULL,                   -- 例 2026-W47
  paper_id      uuid REFERENCES papers(id),
  title         text NOT NULL,
  body          jsonb NOT NULL,                  -- 企画の16項目
  rank_score    numeric(6,3),
  status        text NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed', 'selected', 'dismissed', 'expired')),
  generated_by  text NOT NULL,
  origin        knowledge_origin NOT NULL DEFAULT 'ai_inference',
  execution_mode execution_mode NOT NULL DEFAULT 'real',
  created_at    timestamptz NOT NULL DEFAULT now(),
  selected_at   timestamptz,
  tsv           tsvector GENERATED ALWAYS AS (
                  to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(body::text, ''))
                ) STORED,
  UNIQUE (batch_key, paper_id)
);
CREATE INDEX research_proposals_status_idx ON research_proposals (status, created_at DESC);
CREATE INDEX research_proposals_tsv_idx ON research_proposals USING gin (tsv);

CREATE TABLE research_projects (
  id             text PRIMARY KEY,               -- 研究 ID（例 RP-20261123-a1b2c3）。Workflow ID にも使う
  proposal_id    uuid UNIQUE REFERENCES research_proposals(id),  -- 1企画につき1研究（重複起動の防止）
  title          text NOT NULL,
  workflow_id    text UNIQUE NOT NULL,
  status         text NOT NULL DEFAULT 'registered'
                 CHECK (status IN ('registered', 'running', 'paused', 'on_hold',
                                   'completed', 'failed', 'cancelled')),
  phase          text NOT NULL DEFAULT 'registered',
  outcome        text CHECK (outcome IN ('publishable', 'publishable_with_revisions',
                                         'not_publishable', 'failed', 'on_hold', 'cancelled')),
  outcome_reason text,
  execution_mode execution_mode NOT NULL DEFAULT 'real',
  engine         text NOT NULL,                  -- native / ai_scientist_v3 / mock
  budget_usd     numeric(10,2) NOT NULL,
  policy_sha256  text NOT NULL,                  -- 開始時に固定した品質ゲート設定のハッシュ
  workspace_path text,
  branch         text,
  pr_url         text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  started_at     timestamptz,
  finished_at    timestamptz,
  tsv            tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(title, ''))) STORED
);
CREATE INDEX research_projects_status_idx ON research_projects (status, created_at DESC);

-- 事前登録（実験前に固定した内容。ハッシュと Git コミットで後からの変更を検出する）
CREATE TABLE preregistrations (
  id             bigserial PRIMARY KEY,
  project_id     text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  cycle          integer NOT NULL,
  content        jsonb NOT NULL,
  content_sha256 text NOT NULL,
  git_commit     text,
  registered_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, cycle)
);

CREATE TABLE hypotheses (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  label           text NOT NULL,                 -- H1 など
  cycle           integer NOT NULL DEFAULT 1,
  parent_id       uuid REFERENCES hypotheses(id),
  statement       text NOT NULL,
  decision_rule   text,                          -- 成功・失敗の判定基準
  kind            text NOT NULL CHECK (kind IN ('preregistered', 'exploratory')),
  status          text NOT NULL DEFAULT 'untested'
                  CHECK (status IN ('untested', 'supported', 'not_supported', 'inconclusive')),
  preregistration_id bigint REFERENCES preregistrations(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  tsv             tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(statement, ''))) STORED,
  UNIQUE (project_id, cycle, label)
);
CREATE INDEX hypotheses_tsv_idx ON hypotheses USING gin (tsv);

CREATE TABLE experiments (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  cycle           integer NOT NULL DEFAULT 1,
  kind            text NOT NULL CHECK (kind IN ('pilot', 'main', 'ablation', 'sensitivity',
                                                'replication', 'exploratory')),
  name            text NOT NULL,
  status          text NOT NULL DEFAULT 'planned'
                  CHECK (status IN ('planned', 'running', 'completed', 'failed', 'skipped')),
  config          jsonb NOT NULL DEFAULT '{}',
  design          text,                          -- 実験設計の要約（記憶の再利用で検索する）
  seed            bigint,
  cost_usd        numeric(10,4),
  duration_s      numeric(12,1),
  log_uri         text,
  git_commit      text,
  execution_mode  execution_mode NOT NULL DEFAULT 'real',
  idempotency_key text UNIQUE NOT NULL,
  started_at      timestamptz,
  finished_at     timestamptz,
  tsv             tsvector GENERATED ALWAYS AS (
                    to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(design, ''))
                  ) STORED
);
CREATE INDEX experiments_project_idx ON experiments (project_id, cycle);
CREATE INDEX experiments_tsv_idx ON experiments USING gin (tsv);

CREATE TABLE observations (
  id            bigserial PRIMARY KEY,
  project_id    text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  experiment_id uuid REFERENCES experiments(id) ON DELETE CASCADE,
  content       text NOT NULL,
  data          jsonb NOT NULL DEFAULT '{}',
  origin        knowledge_origin NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE evaluation_results (
  id            bigserial PRIMARY KEY,
  project_id    text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  experiment_id uuid REFERENCES experiments(id) ON DELETE CASCADE,
  hypothesis_id uuid REFERENCES hypotheses(id),
  metric        text NOT NULL,
  condition     text NOT NULL DEFAULT '',
  value         double precision,
  ci_low        double precision,
  ci_high       double precision,
  effect_size   double precision,
  p_value       double precision,
  n             integer,
  analysis_kind text NOT NULL CHECK (analysis_kind IN ('confirmatory', 'exploratory')),
  details       jsonb NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (experiment_id, metric, condition, analysis_kind)
);

-- ---------------------------------------------------------------- 監査と品質ゲート

CREATE TABLE audit_runs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  kind            text NOT NULL CHECK (kind IN ('research', 'publication_quality')),
  round           integer NOT NULL,
  auditor         text NOT NULL,                 -- 監査担当（例 codex:<model>）
  implementer     text NOT NULL,                 -- 研究実装担当（監査担当と別であること）
  skill           text NOT NULL,                 -- 使った Skill の名前と版（ハッシュ）
  target_commit   text,
  status          text NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'completed', 'partial', 'failed')),
  research_verdict     text,                     -- 研究妥当性の総合判定
  publication_verdict  text,                     -- 発信可否
  score           numeric(6,2),
  report_uri      text,
  summary         text,
  execution_mode  execution_mode NOT NULL DEFAULT 'real',
  idempotency_key text UNIQUE NOT NULL,
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  UNIQUE (project_id, kind, round),
  CHECK (auditor <> implementer)
);

CREATE TABLE audit_claims (
  id            bigserial PRIMARY KEY,
  audit_run_id  uuid NOT NULL REFERENCES audit_runs(id) ON DELETE CASCADE,
  claim_id      text NOT NULL,
  claim_text    text NOT NULL,
  is_primary    boolean NOT NULL DEFAULT false,
  status        text NOT NULL CHECK (status IN ('Verified', 'Supported', 'Inconclusive',
                                                'Contradicted', 'Not Tested')),
  recalculation_level text,
  limitations   text,
  UNIQUE (audit_run_id, claim_id)
);

CREATE TABLE audit_findings (
  id            bigserial PRIMARY KEY,
  audit_run_id  uuid NOT NULL REFERENCES audit_runs(id) ON DELETE CASCADE,
  project_id    text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  issue_id      text NOT NULL,
  severity      text NOT NULL CHECK (severity IN ('Critical', 'Major', 'Minor', 'Improvement')),
  certainty     text NOT NULL DEFAULT 'Confirmed' CHECK (certainty IN ('Confirmed', 'Suspected')),
  category      text,
  title         text NOT NULL,
  detail        jsonb NOT NULL DEFAULT '{}',     -- 場所・期待と観測・再現手順・修正案など
  status        text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'fix_attempted', 'resolved', 'unresolved')),
  resolved_in_round integer,
  created_at    timestamptz NOT NULL DEFAULT now(),
  tsv           tsvector GENERATED ALWAYS AS (
                  to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(detail::text, ''))
                ) STORED,
  UNIQUE (audit_run_id, issue_id)
);
CREATE INDEX audit_findings_project_idx ON audit_findings (project_id, severity, status);
CREATE INDEX audit_findings_tsv_idx ON audit_findings USING gin (tsv);

CREATE TABLE audit_recomputed_metrics (
  id            bigserial PRIMARY KEY,
  audit_run_id  uuid NOT NULL REFERENCES audit_runs(id) ON DELETE CASCADE,
  claim_id      text,
  metric        text NOT NULL,
  condition     text,
  original_value    double precision,
  independent_value double precision,
  difference    double precision,
  tolerance     double precision,
  within_tolerance boolean,
  interpretation text
);

-- 品質ゲートの判定記録（どの設定で判定したかをハッシュで残す）
CREATE TABLE gate_evaluations (
  id            bigserial PRIMARY KEY,
  project_id    text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  gate          text NOT NULL,                   -- research / publication
  round         integer NOT NULL,
  passed        boolean NOT NULL,
  policy_sha256 text NOT NULL,
  checks        jsonb NOT NULL,                  -- [{name, passed, detail}]
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, gate, round)
);

-- ---------------------------------------------------------------- 記憶・成果物・発信

CREATE TABLE lessons_learned (
  id          bigserial PRIMARY KEY,
  project_id  text REFERENCES research_projects(id) ON DELETE SET NULL,
  category    text NOT NULL,                     -- design / evaluation / failure / cost / audit など
  content     text NOT NULL,
  tags        text[] NOT NULL DEFAULT '{}',
  origin      knowledge_origin NOT NULL DEFAULT 'ai_inference',
  verification verification_status NOT NULL DEFAULT 'unverified',
  created_at  timestamptz NOT NULL DEFAULT now(),
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED
);
CREATE INDEX lessons_tsv_idx ON lessons_learned USING gin (tsv);
CREATE INDEX lessons_tags_idx ON lessons_learned USING gin (tags);

CREATE TABLE research_artifacts (
  id          bigserial PRIMARY KEY,
  project_id  text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  kind        text NOT NULL,                     -- code / data / log / figure / report / audit / eval_code
  path        text NOT NULL,                     -- 作業領域からの相対パス、または外部 URI
  sha256      text,
  size_bytes  bigint,
  git_commit  text,
  description text,
  reusable    boolean NOT NULL DEFAULT false,    -- 評価コードなど、次の研究で再利用できるもの
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, path)
);

CREATE TABLE publication_drafts (
  id          bigserial PRIMARY KEY,
  project_id  text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  kind        text NOT NULL,                     -- report_md / report_html / talk_script / readme / audit_report / future_work
  path        text NOT NULL,
  sha256      text,
  status      text NOT NULL DEFAULT 'draft'
              CHECK (status IN ('draft', 'gate_passed', 'approved', 'published', 'withheld')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, kind)
);

-- 公開の承認（発信先ごとに拡張できる。現在の発信先は github_pr のみ）
CREATE TABLE publication_approvals (
  id          bigserial PRIMARY KEY,
  project_id  text NOT NULL REFERENCES research_projects(id) ON DELETE CASCADE,
  channel     text NOT NULL,                     -- github_pr / （将来）linkedin など
  decision    text NOT NULL CHECK (decision IN ('auto_allowed', 'approved', 'rejected', 'pending')),
  decided_by  text NOT NULL,                     -- policy / user
  detail      jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, channel)
);

-- 意味検索用の埋め込み（対象の種類をまたいで1表で持つ）
CREATE TABLE embeddings (
  id           bigserial PRIMARY KEY,
  entity_type  text NOT NULL,                    -- paper / proposal / hypothesis / experiment / finding / lesson
  entity_id    text NOT NULL,
  model        text NOT NULL,
  content_sha256 text NOT NULL,
  embedding    vector(384) NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entity_type, entity_id, model)
);
CREATE INDEX embeddings_hnsw_idx ON embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX embeddings_entity_idx ON embeddings (entity_type);

-- ---------------------------------------------------------------- 運用

-- 費用台帳。従量課金と、定額契約の利用量を API 定価に換算した推定値を区別する
CREATE TABLE cost_ledger (
  id              bigserial PRIMARY KEY,
  project_id      text REFERENCES research_projects(id) ON DELETE SET NULL,
  category        text NOT NULL CHECK (category IN ('claude', 'openai', 'other_model',
                                                    'embedding', 'data_api', 'compute', 'cloud')),
  billing         text NOT NULL CHECK (billing IN ('metered', 'subscription_equivalent', 'free')),
  provider        text,
  model           text,
  purpose         text NOT NULL,
  amount_usd      numeric(12,6) NOT NULL,
  is_estimate     boolean NOT NULL DEFAULT false,
  usage           jsonb NOT NULL DEFAULT '{}',
  idempotency_key text UNIQUE NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX cost_ledger_project_idx ON cost_ledger (project_id);

-- 副作用の実行記録（再試行で GitHub 操作などが重複しないようにする）
CREATE TABLE side_effects (
  idempotency_key text PRIMARY KEY,
  kind            text NOT NULL,
  result          jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workflow_events (
  id          bigserial PRIMARY KEY,
  project_id  text REFERENCES research_projects(id) ON DELETE CASCADE,
  workflow_id text NOT NULL,
  event       text NOT NULL,
  detail      jsonb NOT NULL DEFAULT '{}',
  idempotency_key text UNIQUE,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX workflow_events_project_idx ON workflow_events (project_id, created_at);

CREATE TABLE notifications (
  id          bigserial PRIMARY KEY,
  project_id  text REFERENCES research_projects(id) ON DELETE CASCADE,
  level       text NOT NULL CHECK (level IN ('info', 'success', 'warning', 'error')),
  title       text NOT NULL,
  body        text NOT NULL DEFAULT '',
  idempotency_key text UNIQUE NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  read_at     timestamptz
);

-- 人間の介入（自律性の指標に使う）
CREATE TABLE human_interventions (
  id          bigserial PRIMARY KEY,
  project_id  text REFERENCES research_projects(id) ON DELETE CASCADE,
  kind        text NOT NULL,                     -- select / pause / resume / cancel / approve / budget_change
  detail      jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);
