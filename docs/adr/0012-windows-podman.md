# ADR-0012: 社内の Windows PC と Podman への移行 — 実行方式、隔離、プロセスの管理

- 日付: 2026-11-26
- 状態: 提案。**実行方式（下の A / B）は未確定**（社内での WSL2・Hyper-V・Podman の許可が未確認）。
  コードは A・B のどちらでも動く形で用意した。Windows の実機と、Windows 上の Podman では、まだ何も確かめていない。

## 背景

開発と運用の対象を、個人の macOS から社内の Windows PC に移す。コンテナは Docker ではなく Podman を使う。
これまでの実装は、次の点で macOS と Docker に依存していた（一覧は [docs/windows/os-dependencies.md](../windows/os-dependencies.md)）。

- 実験の隔離: `docker run`、または macOS の `sandbox-exec`。
- 改善候補の実行（ADR-0011）: macOS なら `sandbox-exec`、それ以外は「別プロセスにするだけ」。
- プロセスの回収: `os.killpg` と POSIX のシグナル。ワーカーの停止は `loop.add_signal_handler`。
- 起動・停止: Bash の `scripts/aarctl` と Docker Compose。スリープの検出と抑止は `pmset` と `caffeinate`。

## 決定

### 1. 実行方式は、社内の許可を確かめてから 1 つ選ぶ

| | 方式 | 選ぶ条件 |
|---|---|---|
| A | WSL2 の許可されたディストリビューションの中に、Python・Temporal・実行担当の CLI を揃える。Podman は、同じディストリビューションで動かすか、Podman Desktop の仮想環境へ接続するかを明示して決める | WSL2 が社内で許可されている |
| B | Windows ネイティブの Python と PowerShell ＋ 社内で許可された Podman の Linux 仮想環境 | WSL2 は不可だが、Podman の仮想環境（Hyper-V など）は許可されている |
| — | ローカルでの安全な実行は**未成立**と報告する | どちらの仮想化も許可されない |

- **第一候補は A**。理由: 基盤の主経路（POSIX のプロセス管理、Podman のローカル接続、パスの扱い）が Linux と同じになり、
  確かめる範囲が狭い。Claude Code の公式の要件でも、OS によるサンドボックスはネイティブの Windows では使えず、WSL2 では使える
  （候補を作る側の隔離の前提になる。2026-11-26 に https://code.claude.com/docs/en/setup を確認）。
- B のとき、Podman Desktop の公式の手順では、仮想環境の提供元は WSL2 か Hyper-V の 2 つ。WSL2 が不可なら Hyper-V が要る
  （Windows の Pro / Enterprise、Hyper-V の有効化と仮想環境の作成に管理者権限）。Hyper-V も使えるとは決めつけない。
- どちらも許可されない場合に、「別プロセスだけの実行」へ自動で切り替えることはしない。実装上も、切り替える経路はない（下の 3）。
- 「技術的に導入できる」と「社内で許可されている」は別に記録する。前者は `scripts/windows/Probe-Environment.ps1`（読み取りだけ）で集め、
  後者は情報システム部門の回答を [docs/windows/setup.md](../windows/setup.md) の表に書く。
- 最初から A・B の両方を正式に対応とはしない。選んだ一方で実機の検証を終えてから、この ADR の状態を「採用」にする。

### 2. コンテナは `podman` を明示して呼ぶ。Compose は使わない

- `aar.container` が `podman`（設定した実行ファイルと接続先）を直接呼ぶ。`docker` の別名や、Docker 互換のソケットには頼らない。
- PostgreSQL は `podman run` で起動する（`aar.ctl`）。`podman compose` は外部の provider（docker-compose が入っていればそれが優先）を
  呼ぶ仕組みなので、Docker Compose への暗黙の依存を避けるために使わない。healthcheck は `podman healthcheck run` を直接呼んで待つ
  （定期実行のタイマーに依存しない）。公開は `127.0.0.1` だけ。
- `infra/docker-compose.yml` と Bash の `scripts/aarctl` は、旧環境（macOS）専用として残す。
- イメージの形式や `Dockerfile` という名前までは排除しない。取り除くのは、Docker Engine / Docker Desktop への実行時の依存。
- 方針ファイルの `safety.sandbox: docker` は「コンテナで隔離する」の意味として読み、エンジンは環境の設定（`AAR_CONTAINER_ENGINE`、
  既定は `podman`）で決める。**方針ファイルは変更していない**ので、既存の研究に固定したハッシュは変わらない（下の 6）。

### 3. 隔離は、効いていることを確かめた構成でだけ使う

- 隔離の指定（ネットワークなし、ルートは読み取り専用、メモリ・CPU・プロセス数の上限、capability の全削除、権限昇格の禁止、非 root）は
  `container.hardening_flags` の 1 か所で組み立てる。
- `aar podman verify` が、同じ指定でコンテナを動かし、コンテナの中から制約を確かめる（cgroup の上限値、`CapEff`、`NoNewPrivs`、
  インターフェース、書き込みの可否、UID、マウントの読み書き、時間切れ後にコンテナが残らないこと）。結果は `var/podman/` に記録する。
- 記録は、接続先・版・rootless かどうか・cgroups・イメージ・隔離の指定から作る指紋に結び付く。どれかが変われば、検証し直す。
- **検証に通った記録のない構成では、実験も改善候補の評価も実行しない**（`IsolationUnavailable`）。弱い方式へは切り替えない。
  指定を受け付けても効かない構成がある（公式の説明でも、cgroups v1 の rootless では `--memory` と `--cpus` が使えない）。
- rootless かどうかは、`podman info` の値をそのまま記録する。確かめずに rootless とは書かない。
- コンテナは名前を付けて起動し、ID も記録する。正常終了・時間切れ・キャンセルのどれでも、名前で強制削除して、残っていないことを確かめる。
  残った実行は、成功として扱わない。

### 4. 改善候補の評価（ADR-0011 の隔離の置き換え）

**候補を実行する側**（評価）は、次の形にした。

- 内容を固定したイメージ（`AAR_IMPROVE_IMAGE`。ダイジェストつきでなければ拒否）の Podman コンテナで動かす。
- マウントを 1 つも持たせない。候補のファイル、信頼する実行用のコード（`child.py`）、課題の入力は、標準入力で渡す。
  評価器、正解、採否の基準、研究の作業領域、基盤のチェックアウト、認証情報は、コンテナに存在しない。
- ネットワークなし、読み取り専用、nobody、資源の上限つき。書けるのは、コンテナの中の一時領域（tmpfs）だけ。
- 評価する側は、候補のモジュールを自分のプロセスに import しない（従来どおり）。現行方式と候補は、同じ関数・同じ条件で動く。
- 計画が求める隔離の既定を `podman` にした。`seatbelt`（旧環境）と `process`（別プロセスだけ）の実行では、採否は保留になる。
  `python -I -S` は、コンテナの中で追加の対策として使い続ける。
- これは評価器の変更なので、評価器のハッシュが変わる。**固定済みの評価計画での新しい実行は「評価器が変わった」として比較されない**
  （ADR-0011 の決まりどおり）。最初の候補（`IC-20261124-6e4bf7`）の棄却と、採否の基準（長さの上限 4.0 倍など）は変えていない。

**候補を作る側**（実行担当の CLI）の隔離は、**未完成**。

- 目標: 基盤の作業用の複製と、許可した開発用の資料だけを見せる。本番の DB、研究の原本、認証情報、封印評価の課題、Podman のソケット、
  ホスト全体は渡さない。AI の認証は、会社で承認された最小の範囲で渡す。
- 実現の案: A なら、専用のコンテナ（または専用の Linux 利用者）で CLI を動かし、複製だけをマウントして、通信先をモデルの提供元に限る。
  B は、ネイティブの Windows で Claude Code の OS サンドボックスが使えないので、コンテナの中で CLI を動かす案だけが残る。
  どちらも、会社が承認する認証の渡し方と通信先が決まらないと設計を確定できない。
- それまでは、**既定で候補の生成を拒否する**（`generation_refused` を台帳に記録）。人が `AAR_IMPROVE_ALLOW_UNISOLATED_GENERATOR=1` を
  設定したときだけ生成し、「隔離なし」と記録する。次の候補の生成は、今回の移行の範囲外。

### 5. プロセスの管理（`aar.proc`）

- POSIX: 新しいセッションで起動し、プロセスグループへ SIGTERM、応じなければ SIGKILL。
- Windows: `os.killpg` も POSIX のシグナルも使わない。停止した状態で起動（`CREATE_SUSPENDED`）し、Job Object
  （`KILL_ON_JOB_CLOSE`）に入れてから再開する。止めるときは Job ごと終了させ、残りのプロセス数を Job に問い合わせる。
  Job Object はプロセスの管理であり、コードの隔離ではない。
- イベントループは既定（Proactor）のまま。子プロセスを扱えないループには変えない。
- 親が先に終わり、出力のパイプを握った孫が残る場合も、孫を止めてから戻る（移行のテストで見つけた。従来は待ち続けた）。
- 引数は並びで渡し、シェルの文字列にしない。Windows の `.cmd` のランチャーは直接は起動しない（cmd.exe が引数を解釈し直す）。
  npm の定型のランチャーなら、その先の `node <script>` を起動する。定型でなければ、推測せずに失敗させる。
- ワーカーの停止: Windows では `add_signal_handler` が使えないので、`signal.signal` と、停止の依頼のファイル（`var/run/worker.stop`）で受ける。

### 6. 設定と、既存の研究に固定した方針のハッシュ

- Podman の設定（エンジン、実行ファイル、接続先、イメージ）は、環境の設定（`AAR_…`、`.env`）に置いた。
  方針ファイルを書き換えると、既存の研究はすべて「方針の変更」で保留になるので、今回は書き換えない。
- 方針ファイルの鍵の名前（`docker_image` など）を直すのは、切り替えのあとの別の変更にする。そのときは、研究ごとに人が
  `aar projects adopt-policy` で取り込み、前後のハッシュを記録する。記録済みのハッシュは上書きしない。
- 旧環境（macOS）でこのコードを動かす場合は、`.env` に `AAR_CONTAINER_ENGINE=docker` を明示する。
  明示しないと Podman を探し、見つからなければ実験を拒否する（黙って Docker には戻らない）。

### 7. 時刻

- DB に保存する時刻は UTC（従来どおり）。表示と、日次・週次の区切りは、設定したタイムゾーン（`AAR_TIMEZONE`。未設定なら OS の設定、
  定期実行は従来どおり Asia/Tokyo）。
- 実行担当の CLI が表示する利用上限の解除時刻は、その CLI が動く OS の設定で読む（設定したタイムゾーンでは読まない）。
  両者に時差があれば `aar doctor` が指摘する。WSL のディストリビューションのタイムゾーンは、Windows と一致させる。

### 8. スリープ

- `pmset` と `caffeinate` は、macOS 以外では呼ばない。Windows の電源の設定は変更しない（スリープの抑止は実装していない）。
- 判定は 3 値: `slept`（電源の記録にスリープがある）/ `awake`（記録を読めて、スリープがない）/ `unknown`（読めない）。
- Windows は、システムのイベントログの Kernel-Power（42 / 107、モダンスタンバイは 506 / 507）を `wevtutil` で読む。
  WSL からは Windows 側の `wevtutil.exe` を呼ぶ。画面のロックと画面の消灯は、これらのイベントを出さないので、スリープに数えない。
  処理の遅れや時刻の飛びからは推定しない。
- WSL や Podman の仮想環境だけが止まった場合は、ホストの電源の記録に現れない。これは検出していない（未対応）。
- `unknown` を「スリープなし」とは記録しない。工程が失敗して電源の記録を読めなかった場合は、失敗と決めずに保留にする
  （理由は `host_sleep_unknown`。既定で有効、`AAR_HOLD_ON_UNKNOWN_SLEEP`）。スリープ・利用上限・認証・コードの失敗は、別の理由として残る。
- ワークフローの分岐の追加は `workflow.patched("host-sleep-unknown")` で囲んだ。既存の履歴の再生には影響しない。

### 9. DB の実行用の役割

追記だけの表は、トリガーで UPDATE と DELETE を拒否している。ただし、表の所有者はトリガーを無効にできる。これまでアプリは
所有者（コンテナの管理者）で接続していた。新しい環境では、移行を適用する接続（`AAR_MIGRATION_DATABASE_URL`、所有者）と、
実行用の接続（`AAR_DATABASE_URL`、役割 `aar_app`）を分ける。`aar_app` は、追記だけの表に SELECT と INSERT しか持たず、
トリガーの無効化・削除、TRUNCATE、表の作成、所有者への切り替えができない（自動テストで確認）。

## 確かめたこと・確かめていないこと

| 対象 | 状態 |
|---|---|
| OS に依らない部分（プロセスの回収、Podman の呼び出しの組み立て、検証の判定、パスの検出、電源の記録の解釈、起動・停止） | 自動テスト（macOS で実行。CI の Linux と Windows でも実行） |
| Windows の Job Object による回収、PowerShell の入口、改行と文字コード | CI の Windows の実行環境（GitHub のもの。社内の PC ではない） |
| 実物の Podman での隔離 | CI の Linux（rootless）。**Windows 上の Podman の仮想環境では未確認** |
| 社内の Windows PC での起動、WSL2・Hyper-V、リモート接続でのマウント、プロキシと証明書、実行担当の CLI の認証 | **未確認**（実機が要る） |
| スリープ → 復帰 → 自動継続 | **未実施**。人が手で復帰できる時間を決めてから行う |

CI の結果は、PR の説明に書く。**移行は完了していない。** 長期の無人運転は、この移行では承認しない。

## 参照した公式の情報（2026-11-26 に確認）

- Podman Desktop の Windows への導入: https://podman-desktop.io/docs/installation/windows-install
  （Windows 10 ビルド 19043 以降か Windows 11。仮想環境に 6GB のメモリ。WSL2 か Hyper-V。機能の有効化と Hyper-V の仮想環境の作成に管理者権限）
- 別の WSL のディストリビューションからの接続: https://podman-desktop.io/docs/podman/accessing-podman-from-another-wsl-instance
  （ソケットは `/mnt/wsl/podman-sockets/podman-machine-default/podman-root.sock` と `podman-user.sock`。グループの追加が要る）
- podman compose: https://docs.podman.io/en/latest/markdown/podman-compose.1.html （外部の provider を呼ぶ。docker-compose が優先）
- podman run: https://docs.podman.io/en/latest/markdown/podman-run.1.html
  （`--memory`・`--cpus` は cgroups v1 の rootless では使えない。リモートでは、volume は接続先の側でマウントされる）
- 社内での WSL の管理: https://learn.microsoft.com/en-us/windows/wsl/enterprise
  （Intune とグループポリシーで制御できる。利用できるディストリビューションの制限と、root の制限は、標準では提供されない）
- asyncio の Windows での制約: https://docs.python.org/3/library/asyncio-platforms.html
- Job Object: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects （入れ子は Windows 8 以降。入れたプロセスは外せない）
- Claude Code の要件: https://code.claude.com/docs/en/setup
- Temporal の版の管理: https://docs.temporal.io/develop/python/workflows/versioning
