# autonomous-ai-research

最新の AI 研究を集め、検証する価値のある企画を提案し、選ばれた企画について実験・独立監査・公開準備までを自動で進める基盤。
利用者が普段行う操作は、検証企画を選ぶことだけ。

状態: 初期バージョン（2026-11-23）。個人の PC（macOS）で動かす前提。動作を確認した範囲と未確認の範囲は
[docs/STATUS.md](docs/STATUS.md) に分けて書いてある。

## 何をするか

```
論文の収集（日次）  arXiv / OpenAlex / Semantic Scholar → 重複を除いて保存 → 価値評価
        ↓
検証企画の生成（週次）  上位の論文から、個人の PC で確かめられる企画を作る
        ↓
利用者が企画を選ぶ  ← 操作はここだけ
        ↓
研究（自動）  原論文の整理 → 研究設計 → 事前登録 → 実装 → 予備実験 → 本実験 → 分析 →（必要なら次の仮説へ）
        ↓
独立監査（自動）  実装とは別のエージェントが生データから再計算 → 重大な指摘があれば修正・再実験・再監査
        ↓
公開品質の監査と改善（自動）  README・再現手順・ライセンス・秘密情報・主張と数値の一致
        ↓
成果物  技術レポート（Markdown / HTML）、比較グラフ、発表原稿、README、監査報告、今後の課題、PR（設定した場合）
```

途中で止まっても（PC の再起動、利用上限、ネットワーク断）、続きから再開する。
認証切れ・費用上限・方針の変更など、自動で進めるべきでない事象では研究を保留にして通知する。

## 構成

| 役割 | 採用 | 補足 |
|---|---|---|
| 進行管理 | Temporal | 研究1件が1ワークフロー。単体サーバー＋SQLite で永続化（[ADR-0002](docs/adr/0002-temporal-deployment.md)） |
| 研究の実装 | Claude Code（`claude -p`） | 読み書きの道具だけを渡す。シェルは渡さない |
| 独立監査 | Codex（`codex exec`） | 対象の複製を読み、生データから再計算する |
| 研究エンジン | 内蔵（事前登録つきの仮説検証） | AI Scientist v3 はアダプターのみで未接続（[ADR-0003](docs/adr/0003-research-engine.md)） |
| 研究記憶 | PostgreSQL 17 + pgvector | キーワード・ベクトル・絞り込み・引用関係のハイブリッド検索 |
| 実験の隔離 | Docker または macOS の sandbox-exec | ネットワークなし（[ADR-0006](docs/adr/0006-security.md)） |
| UI | FastAPI（サーバー側で描画）と CLI | `http://127.0.0.1:8765` |

設計の理由は [docs/adr/](docs/adr/) に、全体の説明は [docs/architecture.md](docs/architecture.md) にある。

## 必要なもの

- macOS、Python 3.11 以上、Docker Desktop、Git、GitHub CLI（`gh`）
- Temporal CLI（`brew install temporal`）
- Claude Code と Codex の CLI（どちらもログイン済みであること。API キーは使わない）
- 研究独立監査 Skill（[skills/README.md](skills/README.md)）

## 導入

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
scripts/install_skills.sh <研究独立監査 Skill の zip>
scripts/aarctl start          # PostgreSQL・Temporal・ワーカー・UI を起動
.venv/bin/aar doctor          # 足りないものを確認
```

## 使い方

```sh
aar ingest                    # 論文を今すぐ収集（通常は毎日 6:30 に自動）
aar proposals generate        # 企画を今すぐ生成（通常は毎週月曜 7:30 に自動）
aar proposals list            # 企画の一覧
aar select 1                  # 企画を選ぶ。以降は自動で進む
aar projects list             # 研究の一覧
aar projects show <研究ID>    # 進捗、監査、品質ゲート、成果物
aar search "<語句>"           # 研究履歴の検索
aar untested                  # まだ検証されていない課題
aar metrics                   # システムの自己評価
```

同じことは Web UI でもできる。

止める・再開する:

```sh
scripts/aarctl stop           # ワーカー・UI・Temporal を止める（--all で PostgreSQL も）
scripts/aarctl start          # 再開。実行中だった研究は続きから進む
aar projects pause|resume|cancel <研究ID>
aar projects budget <研究ID> <ドル>   # 費用上限に達して保留になったとき
```

成果物は `workspaces/<研究ID>/repo/`（研究ごとの Git リポジトリ、ブランチ `research/<研究ID>`）にできる。
`aar select 1 --remote-repo <owner>/<name>` で非公開のリポジトリを指定すると、品質ゲートの通過後に PR を作る。
指定しなければローカルのブランチに留まる。main へは push しない。

## 判断基準を事前に決める

[config/policy.yaml](config/policy.yaml) に、評価の重み、費用と時間の上限、追加実験を続ける条件、品質ゲート、
公開の範囲、隔離の方法をまとめてある。研究の開始時にこのファイルのハッシュを研究に固定し、
途中で書き換えられたら研究を保留にする（改善担当のエージェントが基準を緩められないようにするため）。

## テスト

```sh
.venv/bin/pytest -q                                   # 外部に出ないテスト
AAR_TEST_DATABASE_URL=postgresql://aar:aar@127.0.0.1:55432/aar_test .venv/bin/pytest -q   # DB と Temporal を使うテストも
```

パイプラインのテストはエージェントを台本に置き換えて流れと記録を確かめるもので、研究の中身の質は確かめない。
モックで動かした研究は DB 上で `execution_mode = 'mock'` と記録され、指標にも数えない。

## 費用について

Claude Code と Codex は定額契約のログインで動かす。台帳（`cost_ledger`）に記録される Claude の金額は、
利用量を API の定価に換算した推定値で、実際の請求額ではない。Codex は金額を返さないため、トークン数だけを記録する。
従量課金の API キーは使わず、子プロセスにも渡さない。

## ライセンス

未定（非公開のため LICENSE ファイルは置いていない）。方針は [docs/LICENSE_POLICY.md](docs/LICENSE_POLICY.md)。
