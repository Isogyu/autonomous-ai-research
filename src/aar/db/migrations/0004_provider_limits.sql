-- 実行担当（Codex / Claude Code）の利用上限の状態。
-- 上限に達したら解除の見込み時刻を記録し、その時刻までは相手に問い合わせない（上限を回避するための連打をしない）。
CREATE TABLE provider_limits (
  provider         text PRIMARY KEY,               -- codex / claude
  limited_until    timestamptz NOT NULL,           -- この時刻までは呼び出さない
  source           text NOT NULL,                  -- provider_message（相手が示した時刻） / backoff（示されなかったので間隔を延ばした）
  consecutive_hits integer NOT NULL DEFAULT 1,     -- 成功を挟まずに上限に当たった回数
  first_hit_at     timestamptz NOT NULL DEFAULT now(),
  last_hit_at      timestamptz NOT NULL DEFAULT now(),
  message          text
);
