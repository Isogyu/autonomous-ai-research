# autonomous-ai-research

最新の AI 研究を集め、検証する価値のある企画を提案し、選ばれた企画について実験・独立監査・公開準備までを自動で進める基盤。
利用者が普段行う操作は、検証企画を選ぶことだけ。あわせて、収集・研究した情報を社内向け（Teams）に発信するための下書きとプレビューを作れる。

**動かす環境: 社内の Windows PC と Podman。** macOS と Docker から移している途中で、**Windows の実機では、まだ一度も動かしていない**
（2026-11-26 時点）。この README のコマンドのうち、実機で確かめたものはない。確かめた範囲は、自動テストと、GitHub の CI
（Linux、Windows の実行環境、Linux の Podman）まで。詳しくは [docs/STATUS.md](docs/STATUS.md) の 12 節と
[ADR-0012](docs/adr/0012-windows-podman.md)。

> **ソフトウェアを勝手に入れない。** 下の「必要なもの」は、要るものの一覧であって、導入の指示ではない。
> 社内の PC への導入（インストーラー、winget、pip、npm、コンテナのイメージの取得、Windows の機能の有効化）は、
> どれも社内で許可された方法と取得先で、利用者が承認してから行う。作業を引き継ぐエージェントへの決まりは [CLAUDE.md](CLAUDE.md)。

## 何をするか

```
論文の収集（日次）  arXiv / OpenAlex / Semantic Scholar → 重複を除いて保存 → 価値評価
        ↓
検証企画の生成（週次）  上位の論文から、手元の PC で確かめられる企画を作る
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
| 研究記憶 | PostgreSQL 17 + pgvector | Podman のコンテナで動かす（Compose は使わない）。公開は `127.0.0.1` だけ |
| 実験の隔離 | Podman のコンテナ | ネットワークなし、読み取り専用、資源の上限。**効いていることを検証した構成でだけ実行する**（[ADR-0006](docs/adr/0006-security.md)、[ADR-0012](docs/adr/0012-windows-podman.md)） |
| 基盤の改善 | 論文と実行の記録から候補を作り、隔離した比較評価で採否を決める 1 巡（`aar improve cycle`） | 採用候補を出すところまで。基盤には自動で反映しない。既定の設定では途中で止まる（[docs/improvement.md](docs/improvement.md)、[ADR-0014](docs/adr/0014-improvement-auto-loop.md)） |
| 社内向けの発信 | 投稿候補、図解カード、プレビュー | **送信はしない**（[docs/briefs/README.md](docs/briefs/README.md)） |
| UI | FastAPI（サーバー側で描画）と CLI | `http://127.0.0.1:8765` |

設計の理由は [docs/adr/](docs/adr/) に、全体の説明は [docs/architecture.md](docs/architecture.md) にある。

## 最初にすること（導入の前に）

1. **社内の許可を確かめる。** WSL2 と Hyper-V、Podman の承認済みの構成、プロキシと証明書、パッケージとイメージの取得先、
   GitHub と AI サービスの接続と認証、扱ってよいデータの範囲。一覧は [docs/windows/setup.md](docs/windows/setup.md) の 0 にある。
2. **環境の事実を集める**（読み取りだけ。何も変更しない）。

   ```powershell
   powershell -NoProfile -File .\scripts\windows\Probe-Environment.ps1 > probe.txt
   ```

   結果には利用者名やホスト名が入る。リポジトリには入れない。
3. **実行方式を決める。** WSL2 が許可されていれば A（WSL2 の中に揃える）、WSL2 は不可で Podman の仮想環境が許可されていれば
   B（Windows ネイティブ）。どちらも不可なら、ローカルでの安全な実行は成立しない（別プロセスだけの実行で代用しない）。

権限が足りないときに、設定の回避や証明書の検証の無効化で解決しない。

## 必要なもの

どれも、社内で許可されたものを、許可された方法で用意する。**この一覧は、導入してよいという意味ではない。**

- Windows 10 か 11（64 ビット）。Podman の仮想環境のために、WSL2 か Hyper-V（どちらを使えるかは社内の許可による）
- Python 3.11 以上、Git for Windows、GitHub CLI（`gh`）
- Podman（と、社内の標準なら Podman Desktop）。イメージ: `pgvector/pgvector:0.8.7-pg17`、`python:3.12-slim`
- Temporal CLI（`temporal.exe`）
- Claude Code と Codex の CLI（この PC で新しくログインする。API キーは使わない。**別の PC の認証をコピーしない**）
- 研究独立監査 Skill と GitHub 公開品質監査 Skill（[skills/README.md](skills/README.md)）。どちらも必須
- 図解カードを PNG にする場合だけ: Edge か Chrome（PC に入っているものを使う）

## 導入（Windows ネイティブ方式。WSL2 方式は [docs/windows/setup.md](docs/windows/setup.md) の 2-A）

**次のコマンドは、パッケージを外部から取得する。取得先と実行の可否を、先に確かめる。**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"     # 埋め込みと PDF の本文も使うなら ".[dev,embed,fulltext]"
Copy-Item .env.example .env                                 # 必要な値だけを書く。秘密情報は Git に入れない
```

- 監査 Skill の導入スクリプト（`scripts/install_skills.sh`）は Bash のままで、Windows では確かめていない。
  置き場所は [skills/README.md](skills/README.md) のとおりなので、手で展開してもよい。
- Podman のイメージの取得、隔離の検証、DB の役割の分離は、[docs/windows/setup.md](docs/windows/setup.md) の 3〜4 の順に行う。

```powershell
.\scripts\aarctl.ps1 verify     # 隔離の指定が、この PC のコンテナの中で実際に効くかを確かめて記録する
.\scripts\aarctl.ps1 start      # PostgreSQL（Podman）・Temporal・ワーカー・UI を起動
.\scripts\aarctl.ps1 doctor     # 足りないものと、隔離の検証の状態を確認
```

`verify` に通っていない構成では、実験も改善候補の評価も実行されない（弱い方式には切り替わらない）。

## 使い方

`aar` は `.\.venv\Scripts\aar.exe`（仮想環境を有効にしていれば `aar`）。Python は UTF-8 モードで動かす
（`$env:PYTHONUTF8 = '1'`。`scripts\aarctl.ps1` は設定して起動する）。

```powershell
aar ingest                    # 論文を今すぐ収集（通常は毎日 6:30 に自動。外部の API を呼ぶ）
aar proposals generate        # 企画を今すぐ生成（通常は毎週月曜 7:30 に自動）
aar proposals generate --public-data --paper <論文ID> --batch <名前>   # 公開データセットを使う企画を、論文を指定して作る
aar proposals list            # 企画の一覧
aar select 1                  # 企画を選ぶ。以降は自動で進む
aar projects list             # 研究の一覧
aar projects show <研究ID>    # 進捗、監査、品質ゲート、成果物
aar search "<語句>"           # 研究履歴の検索
aar untested                  # まだ検証されていない課題
aar metrics                   # システムの自己評価
```

同じことは Web UI でもできる。

起動・停止・状態（詳しくは [docs/windows/operations.md](docs/windows/operations.md)）:

```powershell
.\scripts\aarctl.ps1 status            # サービス、残っている実行用のコンテナ、待ち受けの範囲
.\scripts\aarctl.ps1 stop              # ワーカー・UI・Temporal を止める（--all で PostgreSQL も）
.\scripts\aarctl.ps1 start             # 再開。実行中だった研究は続きから進む
.\scripts\aarctl.ps1 restart-worker    # コードや設定を変えたあと、ワーカーだけを入れ替える
.\scripts\aarctl.ps1 logs worker --lines 100
```

Windows のサービスとしては登録していない。PC の再起動やサインアウトのあとは、手で `start` する。
スリープの抑止はしていない（電源の設定は変更しない）。スリープしている間は、待ち時間も進まない。

研究の操作:

```powershell
aar projects pause|resume|cancel <研究ID>
aar projects budget <研究ID> <ドル>   # 費用上限に達して保留になったとき
aar projects reopen <研究ID>          # 公開不可で終わった研究を、未対応の指摘の修正と独立監査からやり直す（基準は同じ）
aar projects reopen <研究ID> --from publication   # 公開品質監査からやり直す（公開可で終わった研究も対象）
aar projects required-check <研究ID>  # 公開前の必須項目（秘密情報・個人情報・第三者の文章・ライセンス・データ）の決定的な検査だけを実行
aar limits                    # Codex・Claude Code の利用上限の状態（解除の見込み時刻までは問い合わせない）
aar limits --clear codex      # 利用枠の回復を自分で確かめたとき
```

利用上限に達した研究は、相手が示した解除の時刻まで待って自動で再開する（1つの工程につき 5 回まで）。
工程が失敗したときに電源の記録を読めなければ、失敗と決めずに保留にする（`host_sleep_unknown`）。

公開データセットを使う研究では、[config/datasets.yaml](config/datasets.yaml) の一覧（取得できることとライセンスを確認済みのもの）を
企画と設計の担当に示す。データは再配布せず、取得元・ハッシュ・ライセンスの記録と、取得し直すスクリプトだけをリポジトリに入れる。

監査と修正の流れを実物で確かめるための道具（本番の研究には書き込まない）:

```powershell
aar dev fixture <公開可で完了した研究ID> --start   # 検証用（FX-…）として複製し、複製にだけ意図的な不具合を入れて、公開品質監査から実行
```

成果物は `workspaces\<研究ID>\repo\`（研究ごとの Git リポジトリ、ブランチ `research/<研究ID>`）にできる。
`aar select 1 --remote-repo <owner>/<name>` で非公開のリポジトリを指定すると、品質ゲートの通過後に PR を作る。
指定しなければローカルのブランチに留まる。main へは push しない。

社内向けの発信（送信はしない。詳しくは [docs/briefs/README.md](docs/briefs/README.md)）:

```powershell
aar brief list                # 投稿候補と状態
aar brief preview             # briefs\<id>\preview\ に、カード・本文・詳細ページ・プレビュー（HTML）を作る
aar brief export-png          # 図解カードを PNG にする（PC に入っているブラウザーを使う）
```

## 判断基準を事前に決める

[config/policy.yaml](config/policy.yaml) に、評価の重み、費用と時間の上限、追加実験を続ける条件、品質ゲート、
公開の範囲、隔離の方法をまとめてある。研究の開始時にこのファイルのハッシュを研究に固定し、
途中で書き換えられたら研究を保留にする（改善担当のエージェントが基準を緩められないようにするため）。

## テスト

```powershell
.\.venv\Scripts\python.exe -m pytest -q -rs          # DB を使わないテスト。-rs で、動かなかったテストと理由が出る
```

- DB と Temporal を使うテストは、`$env:AAR_TEST_DATABASE_URL = 'postgresql://aar:aar@127.0.0.1:55432/aar_test'` を設定して実行する。
  **Windows では、まだ一度も実行していない。** Temporal の開発用サーバーが PATH にないと、テストが実行ファイルを外部から取得しようとする。
  取得してよいかを、先に確かめる。
- 実物の Podman での隔離のテストは、`$env:AAR_PODMAN_TESTS = '1'` と `$env:AAR_TEST_IMAGE = '<ダイジェストつきのイメージ>'` を設定して
  `pytest -q -rA -m podman`。**Windows の Podman では、まだ実行していない。**
- skip は合格ではない。CI は 3 経路（単体、Windows、Podman）に分かれていて、ある経路の合格を別の経路の合格に数えない。

パイプラインのテストはエージェントを台本に置き換えて流れと記録を確かめるもので、研究の中身の質は確かめない。
モックで動かした研究は DB 上で `execution_mode = 'mock'` と記録され、指標にも数えない。

## 旧環境（macOS と Docker）

移行の前は、個人の macOS と Docker Desktop で動かしていた。そのための `scripts/aarctl`（Bash）と `infra/docker-compose.yml` は、
変更せずに残してある。macOS 向けの追加の開発と実地検証は止めている。旧環境でこの版のコードを動かすときは、`.env` に
`AAR_CONTAINER_ENGINE=docker` を書く（書かないと Podman を探し、見つからなければ実験を拒否する）。
旧環境からのデータの移し方と切替は [docs/windows/migration.md](docs/windows/migration.md)（計画。未実行）。

## 費用について

Claude Code と Codex は定額契約のログインで動かす。台帳（`cost_ledger`）に記録される Claude の金額は、
利用量を API の定価に換算した推定値で、実際の請求額ではない。Codex は金額を返さないため、トークン数だけを記録する。
従量課金の API キーは使わず、子プロセスにも渡さない。

## ライセンス

未定（非公開のため LICENSE ファイルは置いていない）。方針は [docs/LICENSE_POLICY.md](docs/LICENSE_POLICY.md)。
