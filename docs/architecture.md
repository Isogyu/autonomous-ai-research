# 全体の構成

## プロセス

```
┌────────────┐   選択・操作    ┌──────────────┐  開始・合図  ┌───────────────────────┐
│ Web UI / CLI │ ─────────────→ │  aar.service  │ ───────────→ │ Temporal（単体サーバー）   │
└────────────┘                 └──────────────┘              │ 履歴: var/temporal.db    │
       │ 読み取り                      │                       └───────────┬───────────┘
       ↓                               ↓ 登録                               │ タスク
┌──────────────────────────────────────────────┐              ┌───────────┴───────────┐
│ PostgreSQL 17 + pgvector（Docker、127.0.0.1:55432） │ ←────────── │ ワーカー（aar worker）     │
│ 論文・企画・研究・仮説・実験・監査・費用・通知・記憶   │   読み書き   │ ワークフロー＋アクティビティ │
└──────────────────────────────────────────────┘              └───────────┬───────────┘
                                                                          │ サブプロセス
                         ┌────────────────────┬───────────────────┬──────┴──────────────┐
                         ↓                    ↓                   ↓                     ↓
                  Claude Code            Codex               隔離実行              git / gh
               （計画・実装・修正・     （独立監査・        （実験・分析・テスト。   （作業領域の履歴、
                 文章。読み書きのみ）    公開品質監査）       ネットワークなし）       PR の作成）
```

## ワークフロー

| ワークフロー | 起動 | 内容 |
|---|---|---|
| `IngestWorkflow` | 毎日 6:30（JST） | 論文の収集、重複の統合、粗い採点、埋め込みの作成 |
| `ProposalWorkflow` | 毎週月曜 7:30（JST） | 候補の選定、LLM による採点、企画の生成、通知 |
| `ResearchWorkflow` | 企画の選択 | 研究1件の全工程。Workflow ID = 研究 ID |

### 研究の工程

```
prepare ─ understand ─┬─ design＋事前登録 ─ implement ⇄ pilot（最大3回）─ main ─ analysis ─┐
                      └──────────── 続ける価値があれば次のサイクル（上限つき）←──────────────┘
   ─ 研究監査 ⇄ 修正（＋再実験）（最大3回）─ 成果物 ─ 公開品質監査 ⇄ 修正（最大2回）─ 提出 ─ 確定
                                                          └ 研究内容に及ぶ修正なら研究監査へ戻る
```

- 追加サイクルに進むかは、方針の基準（情報価値の見込み、残りの費用、時間、サイクル数）だけで決める。
- 仮説が支持されなくても結果は保存され、研究は先へ進む。判定は分析コードが事前登録の基準を当てはめた結果で、
  エージェントは引き下げることしかできない。
- 結果は `publishable`（公開候補あり）、`not_publishable`（品質ゲートを通らなかった）、`failed`、`cancelled` のいずれか。
  途中の保留は `on_hold`。モックの実行は `execution_mode = 'mock'` で区別する。

### 外部からの操作

| 操作 | 仕組み | 効き方 |
|---|---|---|
| 一時停止 | Signal `pause` | 実行中の工程が終わったところで止まる |
| 再開 | Signal `resume` | 一時停止・保留から続行 |
| 中止 | Signal `cancel` | 実行中の工程も打ち切る。記録は残る |
| 費用上限の変更 | Update `change_budget` | 検証つき（0 より大きく 500 ドル以下） |
| 状態の確認 | Query `status` | 工程、サイクル、監査の回数 |

## 作業領域

```
workspaces/<研究ID>/
  repo/                 研究ごとの Git リポジトリ（ブランチ research/<研究ID>）＝ 公開候補
    prereg/cycleN.json    事前登録（コミットとタグで固定）
    experiments/ tests/   実験・分析コードとテスト
    results/cycleN/       pilot / main / analysis（生データ raw.jsonl を含む）
    report/ docs/ audit/  成果物と監査記録
  audits/<種類>-roundN/
    target/               監査対象の複製（監査担当が読む）
    audit/                監査の作業場所（台帳、再計算コード）
```

## モジュール

| パス | 役割 |
|---|---|
| `aar/sources/` | 論文 API、重複判定、取り込み |
| `aar/discovery.py` | 価値評価と企画の生成 |
| `aar/engine/native.py` | 研究エンジン（設計・実装・実行・分析・継続の判断） |
| `aar/engine/fix.py` | 監査の指摘への修正 |
| `aar/engine/ai_scientist.py` | AI Scientist v3 のアダプター（未接続） |
| `aar/audit/` | 研究独立監査と公開品質監査の実行、結果の構造化 |
| `aar/gates.py` | 品質ゲート |
| `aar/publish/` | 表・グラフ・報告書・PR |
| `aar/memory/` | 埋め込みとハイブリッド検索、記憶の再利用 |
| `aar/sandbox.py` `aar/workspace.py` | 隔離実行、作業領域、事前登録、秘密情報の検査 |
| `aar/agents/runner.py` | Claude Code・Codex の実行 |
| `aar/temporal/` | ワークフロー、アクティビティ、ワーカー |
| `aar/service.py` `aar/cli.py` `aar/ui/` | 操作の入口 |
| `aar/mock.py` | モック実行の台本 |

## 発信先の拡張

発信先は `publication_approvals.channel` と方針の `publication.channels` で表す。いまあるのは `github_pr` だけ。
発信先を足すときは、成果物（`publication_drafts`）を読んで提出するアクティビティを1つ加え、
承認の記録を同じ表に残す。LinkedIn への投稿は実装していない。
