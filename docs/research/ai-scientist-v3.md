# 調査記録: AI Scientist v3

調査日 2026-11-23。対象は https://github.com/SakanaAI/AI-Scientist-v3 のコミット `f8ae47d`（コードを読んだだけで、実行はしていない）。
採否の判断は [ADR-0003](../adr/0003-research-engine.md)。

## 確認した事実

| 項目 | 内容 |
|---|---|
| 状態 | 公開、Apache-2.0、0.1.0 Alpha、リリースタグなし。正式名は "The Agentic Scientist"、パッケージ名 `agentic-scientist` |
| CLI | `agsci run`（全フェーズ）、`agsci investigate`（探索だけ）、`agsci doctor`、`agsci inspect`、`agsci review` |
| 設定 | Hydra。`key=value` で上書き |
| フェーズ | task_drafting → investigation → ablations → final_evaluation → posthoc → paper → review → revision |
| 入力 | タスクのディレクトリ: `task.md`（Goal と Rubrics）、`capture.py`、`evaluate.py`、`program_0/program_spec.toml` と初期プログラム |
| 評価 | `evaluate.py` が `metrics.json`（`combined_score`、大きいほど良い）と `correct.json` を出す |
| エージェント | claude / codex / cursor / opencode / gemini / pi。Headless CLI 経由で、常に `--allow yolo`（全権限） |
| 認証 | 定額契約のログインを想定（`HEADLESS_BILLING=subscription`）。コンテナ内の Claude は `CLAUDE_CODE_OAUTH_TOKEN` |
| 出力 | `results/<task>/<agent>/<run_id>/` 以下に状態・履歴・成果物。イベントは JSONL |
| 再開 | `paths.run_dir=<既存>` と `execution.phases=...` でフェーズ単位。investigate は `execution.resume=true` |
| 費用 | 計測のみ（`metrics/cost_summary.json`）。**上限の機能はない** |
| 実行環境 | 既定は Docker（Python 3.12、Node、TeX Live、Chromium などを含むイメージ）。task_drafting・final_evaluation・paper・revision はコンテナ必須 |
| 終了コード | 0 は completed と completed_degraded の両方。成否はイベントの `pipeline_end` で見る |
| 内蔵の査読 | OpenAI の API キーが必須 |

## このシステムから見た評価

- 外側から制御する接点（CLI、上書き、イベント、再開）は揃っており、アダプター方式で統合できる。
- 研究の型が違う。AI Scientist v3 は評価関数の点数を上げる探索で、仮説・判定基準を実験前に固定する工程を持たない。
- 費用の上限がないので、回数と時間の上限を必ず外から与える必要がある。
- 全権限のエージェントをホストで動かすのは危険。上流の README も専用環境を勧めている。

## 導入手順（2026-11-23 に実施）

```sh
git clone https://github.com/SakanaAI/AI-Scientist-v3 vendor/AI-Scientist-v3
git -C vendor/AI-Scientist-v3 checkout f8ae47d
cd vendor/AI-Scientist-v3 && uv sync
uv run agsci doctor --json
uv run agsci run examples/circpack --dry-run     # コマンドを表示するだけ
```

結果: `uv sync` は通り、doctor は macOS の一時領域（`/var/folders` 以下がシンボリックリンクを含む）を拒否した。
`AGENTIC_TEMP_ROOT=<実在のディレクトリ>` を設定すると全項目が通り、dry-run は6フェーズ分のコマンドを表示して終了した。
エージェントを呼ぶ実行（`agsci investigate`）は行っていない。macOS での実動作は未確認のまま
（プロセスの封じ込めは Linux 限定の実装で、上流の文書にも macOS の記載はない）。
