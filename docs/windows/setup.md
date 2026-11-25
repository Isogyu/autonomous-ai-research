# Windows と Podman: セットアップと診断

対象は社内の Windows PC。**この手順は、実機ではまだ一度も通していない**（2026-11-25 時点）。
書いてあるコマンドのうち、CI で動かしたものには（CI）と付けた。それ以外は、実機で最初に確かめることになる。

権限が足りないときに、設定の回避や証明書の検証の無効化（`--tls-verify=false`、`GIT_SSL_NO_VERIFY`、`NODE_TLS_REJECT_UNAUTHORIZED=0`、
`pip --trusted-host` など）で解決しない。止めて、情報システム部門に確認する。

## 0. 最初に確かめること（実行方式を決める）

### 技術的な事実を集める

```powershell
powershell -NoProfile -File .\scripts\windows\Probe-Environment.ps1 > probe.txt
```

読み取りだけを行う（CI）。Windows の版・ビルド・CPU、メモリと空きディスク、仮想化、WSL と Hyper-V の導入の状況、
Podman の版・接続先・rootless かどうか、CLI の実行ファイルの種類、プロキシと証明書の設定の有無、電源のイベントを読めるかを出す。
利用者名・ホスト名・社内の URL が入るので、共有する前に確かめる。**リポジトリには入れない。**

### 社内の許可を確かめる（この表を埋める）

技術的に動くことと、許可されていることは別。回答をここに記録する。

| 項目 | 技術的な状況（probe の結果） | 社内の許可（回答者・日付） |
|---|---|---|
| WSL2 を使ってよいか。使えるディストリビューション | 未確認 | 未確認 |
| Hyper-V を使ってよいか | 未確認 | 未確認 |
| Podman / Podman Desktop の承認済みの版と構成（提供元は WSL2 か Hyper-V か） | 未確認 | 未確認 |
| Podman は rootless か rootful か（`podman info` の値） | 未確認 | 未確認 |
| 管理者権限が要る作業（機能の有効化、Hyper-V の仮想環境の作成、全利用者向けの導入）を誰が行うか | — | 未確認 |
| プロキシ、社内の証明書（Python、Git、Node、Podman のそれぞれ） | 未確認 | 未確認 |
| コンテナのレジストリ（Docker Hub を直接使えるか、社内のミラーか） | 未確認 | 未確認 |
| Python のパッケージの取得先（PyPI を直接使えるか、社内のミラーか） | 未確認 | 未確認 |
| GitHub への接続と認証の方法（`gh auth login` の方式、非公開リポジトリへの接続） | 未確認 | 未確認 |
| Claude Code と Codex を社内 PC で使ってよいか。承認された契約・認証の方法・通信先 | 未確認 | 未確認 |
| 研究の作業内容（複製と作業の説明）がモデルの提供元に送られることの可否 | — | 未確認 |
| 個人の PC から社内の PC へ移してよいデータの範囲 | — | 未確認 |
| スリープの設定を変えずに、長い処理を動かしてよい時間帯 | — | 未確認 |

### 方式を選ぶ（[ADR-0012](../adr/0012-windows-podman.md)）

- WSL2 が許可されている → **A**（WSL2 の中に揃える）。
- WSL2 は不可で、Podman の仮想環境（Hyper-V など）は許可されている → **B**（Windows ネイティブ）。
- どちらも不可 → ここで止める。ローカルでの安全な実行は未成立。別プロセスだけの実行で代用しない。

選んだ方式と理由を ADR-0012 に書き、状態を更新する。

## 1. 共通の準備

1. リポジトリを取得する。改行は変換されない（`.gitattributes` で固定）。確かめる（CI）:
   ```powershell
   git ls-files --eol | Select-String 'w/crlf'      # 何も出なければよい
   ```
2. 秘密情報は、社内の PC で新しく設定する。**Mac の `.env`、`~/.claude`、`~/.codex`、`gh` の認証をコピーしない。**
3. 置き場所は、空白や日本語を含んでもよい（テスト済み、CI）。ただし UNC（`\\server\share`）やネットワークドライブの上には置かない
   （Podman のマウントと Git の挙動を確かめていない）。
4. 封印評価の seed と課題（`var/improve/`）は、本人だけが読める場所に置く。Windows では `chmod` が働かないので、フォルダーのアクセス権で制限する。

## 2-B. Windows ネイティブ方式

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"      # 取得先は、社内で許可されたもの
Copy-Item .env.example .env                                 # 必要な値だけを書く
```

`.env` に書く値（例。値は環境に合わせる）:

```
AAR_CONTAINER_ENGINE=podman
AAR_PODMAN_CONNECTION=            # `podman system connection list` の名前。既定の接続でよければ空
AAR_TIMEZONE=Asia/Tokyo
AAR_TEMPORAL_NAMESPACE=aar-win    # Mac とは別の名前空間
AAR_CLAUDE_BIN=                   # PATH の claude が .cmd のランチャーなら、claude.exe の場所を書く
AAR_CODEX_BIN=
```

- Python は UTF-8 モードで動かす。`scripts\aarctl.ps1` は `PYTHONUTF8=1` を設定して起動する。手で `aar` を呼ぶときも設定する。
- Temporal の CLI（`temporal.exe`）を、社内で許可された方法で導入する。
- Claude Code と Codex は、この PC で新しくログインする。`aar doctor` が、実際に起動する実行ファイルを表示する。

## 2-A. WSL2 方式

WSL のディストリビューションの中で、Linux と同じ手順で準備する（`python3 -m venv .venv`、`.venv/bin/pip install -e ".[dev]"`）。

- リポジトリと `var/`・`workspaces/` は、WSL のファイルシステム（`/home/…`）に置く。`/mnt/c/…` の上には置かない
  （権限と速度、改行の扱いが変わる）。
- Podman の置き場所を 1 つに決めて、`.env` に書く。
  - ディストリビューションの中で Podman を動かす: 接続先は既定のまま。
  - Podman Desktop の仮想環境へ接続する: 公式の手順（`/mnt/wsl/podman-sockets/podman-machine-default/podman-user.sock` か
    `podman-root.sock`、グループへの追加）で接続を登録し、その名前を `AAR_PODMAN_CONNECTION` に書く。
    この場合、**コンテナから見えるのは仮想環境のファイルで、ディストリビューションのパスをそのままマウントできるとは限らない**。
    下の「隔離の検証」が、小さな読み書きで確かめる。通らなければ、実験は実行されない（制御された転送は未実装）。
- タイムゾーンを Windows と一致させる（`aar doctor` が時差を指摘する）。
- Windows の PowerShell から操作するときは、次を設定して `scripts\aarctl.ps1` を使う。
  ```powershell
  $env:AAR_WSL_DISTRO = '<ディストリビューション名>'
  $env:AAR_WSL_REPO = '/home/<利用者>/autonomous-ai-research'
  ```

## 3. Podman の確認と、隔離の検証

```powershell
.\.venv\Scripts\python.exe -m aar.cli podman info      # 版、rootless かどうか、リモートか、cgroups
```

イメージを、社内で許可された取得先から取得する。実行時には取得しない（`--pull never`）。

```powershell
podman pull docker.io/library/python:3.12-slim
podman pull docker.io/pgvector/pgvector:0.8.7-pg17
podman image inspect --format '{{index .RepoDigests 0}}' docker.io/library/python:3.12-slim
```

最後の行の出力（`…@sha256:…`）を、`.env` の `AAR_IMPROVE_IMAGE` に書く（改善候補を実行するイメージ。ダイジェストがないと評価しない）。

```powershell
.\scripts\aarctl.ps1 verify
```

隔離の指定がコンテナの中で実際に効くかを確かめて、`var\podman\` に記録する（Linux の Podman では CI で通過）。
確かめる項目: ネットワークに出られない、ルートに書けない、capability がない、権限昇格の禁止、メモリ・CPU・プロセス数の上限、
root 以外で動く、読み取り専用のマウントに書けない、出力領域に書けてクライアント側から読める（空白・日本語のパス）、
時間切れのあとにコンテナもクライアントのプロセスも残らない。

- **1 つでも通らなければ、その構成では実験も評価も実行されない。** 通らない項目を記録して、構成を見直す（指定を緩めない）。
- 研究ごとの、依存関係つきの実験用イメージは、`aar podman verify --workspace <研究 ID>` で検証する。
- Podman の版、接続先、rootless / rootful、イメージ、資源の上限のどれかを変えたら、検証し直す。

## 4. DB の役割を分けて起動する

```powershell
.\scripts\aarctl.ps1 start        # PostgreSQL（Podman）、Temporal、ワーカー、UI
```

初回は、所有者の接続で移行を適用し、実行用の役割を作る。

```powershell
$env:AAR_MIGRATION_DATABASE_URL = 'postgresql://aar:<所有者のパスワード>@127.0.0.1:55432/aar'
$env:AAR_APP_PG_PASSWORD = '<実行用の役割のパスワード>'
.\.venv\Scripts\python.exe -m aar.cli migrate
```

そのあと、`.env` の `AAR_DATABASE_URL` を `aar_app` の接続にし、`AAR_MIGRATION_DATABASE_URL` を設定したままにする
（ワーカーは移行を適用しなくなり、未適用の移行があれば起動しない）。パスワードは `.env` にだけ置き、Git に入れない。
既定のパスワード（`aar`）は、検証用の DB でだけ使う。

## 5. 診断

```powershell
.\scripts\aarctl.ps1 doctor
.\scripts\aarctl.ps1 status
```

`doctor` で見るもの: PostgreSQL と pgvector の版、**DB の実行用の権限**（所有者で接続していると ✗）、Temporal、
Claude Code・Codex の実行ファイルとログイン、GitHub CLI、**Podman の構成**（rootless / rootful、リモートか、cgroups）、
**隔離の検証の有無**、UTF-8 モード、**ホストの電源の記録を読めるか**、タイムゾーンの時差、方針ファイルのハッシュ。

`status` で見るもの: 各サービス、残っている実行用のコンテナ、各ポートの応答、**ループバック以外で待ち受けていないか**。
待ち受けの確認は、実行した層の中から見た範囲だけ。Windows・WSL・Podman の仮想環境のそれぞれで確かめる。

- Windows: `Get-NetTCPConnection -State Listen -LocalPort 55432,7233,8233,8765 | Select LocalAddress,LocalPort`（`127.0.0.1` と `::1` だけならよい）
- WSL・仮想環境: `ss -ltn | grep -E '55432|7233|8233|8765'`
- 別の PC から、この PC のアドレスの上のポートへ接続できないこと（社内の決まりの範囲で確かめる）

## 6. 新しい環境の検証（本番のデータを入れる前に）

検証用の DB、新しい名前空間、新しい保存先で行う。Mac のデータは、まだ移さない。

1. `.\.venv\Scripts\python.exe -m pytest -q -rs`（DB なし。CI の Windows で通過）。
2. `$env:AAR_TEST_DATABASE_URL = 'postgresql://aar:aar@127.0.0.1:55432/aar_test'` を設定して全テスト（DB あり。**Windows では未実行**。
   Temporal の開発用サーバーを使うテストが Windows で動くかは、ここで初めて分かる）。
3. `$env:AAR_PODMAN_TESTS = '1'; $env:AAR_REQUIRE_PODMAN = '1'; $env:AAR_TEST_IMAGE = '<ダイジェストつきのイメージ>'` で
   `pytest -q -rA -m podman`（**Windows の Podman では未実行**）。
4. `AAR_MODE=mock` で、検証用の研究を一巡させる（外部の API と CLI を呼ばない。原本には触れない）。
5. 実行担当の CLI の認証と動作を、検証用の複製（`aar dev fixture`）1 件で確かめる。利用上限は `aar limits` で先に見る。

結果は、「実装済み」「CI で確認済み」「社内の実機で確認済み」を分けて `docs/STATUS.md` に書く。
