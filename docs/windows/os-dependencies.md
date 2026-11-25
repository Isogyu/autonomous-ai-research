# OS に依存していた箇所の一覧と、移行での扱い

2026-11-26 に、PR #11（`feat/improvement-loop`、未マージ）の上で調べた。区分は次の 4 つ。

- **共通化**: macOS・Linux・Windows で同じコードが動くようにした。
- **移植**: Windows と Podman 向けの実装を足した。
- **旧環境専用**: macOS と Docker 向けの実装を、変更せずに残した。新しい環境では使わない。
- **未対応**: 実装していない。使う場合は、別に作業が要る。

「確認」の列: 自動 = 自動テスト、Win-CI = GitHub の Windows の実行環境、Podman-CI = GitHub の Linux の Podman（rootless）、
実機 = 社内の Windows PC。**実機の欄は、すべて未確認。**

| 箇所 | 依存していたもの | 区分 | 扱い | 確認 |
|---|---|---|---|---|
| `src/aar/sandbox.py` | `docker run` / `docker build` | 移植 | Podman の経路を追加（`aar.container`）。検証に通っていない構成では実行を拒否する | 自動、Podman-CI |
| 同 | `docker` の経路 | 旧環境専用 | `AAR_CONTAINER_ENGINE=docker` を明示したときだけ使う | — |
| 同 | `sandbox-exec`、`/private/tmp/aar-sbx` | 旧環境専用 | macOS 以外では `IsolationUnavailable`。別の方式へは切り替えない | 自動 |
| 同 | `start_new_session`、`os.killpg`、シグナル 15 / 9 | 共通化 | `aar.proc` に集約。Windows は Job Object | 自動、Win-CI |
| 同 | 固定の `PATH`（`/usr/bin:/bin`）、`TMPDIR` | 共通化 | `proc.base_env()`（Windows の起動に要る変数を残す）、`tempfile.gettempdir()` | 自動、Win-CI |
| `src/aar/agents/runner.py` | `os.killpg`、`start_new_session` | 共通化 | `aar.proc.run` を使う。孫まで回収し、残りの数を返す | 自動、Win-CI |
| 同 | CLI を名前（`claude`、`codex`）で起動 | 移植 | `proc.resolve_command`。`.cmd` のランチャーは直接は起動せず、`.exe` か、npm の定型なら `node <script>` を起動する。場所は `AAR_CLAUDE_BIN` / `AAR_CODEX_BIN` で指定できる | 自動。実物の CLI では未確認 |
| 同 | 解除時刻の読み取り（OS のタイムゾーン） | 共通化 | 変更なし。日付つきの表記のテストは維持。表示だけを設定のタイムゾーンにした | 自動 |
| 同 | Codex の OS サンドボックス（`--sandbox read-only`） | 未対応 | Windows での Codex のサンドボックスの挙動は調べていない。実機で確かめる | 実機 |
| `src/aar/temporal/worker.py` | `loop.add_signal_handler(SIGINT, SIGTERM)` | 移植 | Windows は `signal.signal(SIGINT, SIGBREAK)` と、停止の依頼のファイル | 自動（ファイルの経路は実物の Temporal では未確認） |
| `src/aar/limits.py` | `pmset -g log` | 移植 | macOS は従来どおり。Windows と WSL は `wevtutil`（Kernel-Power）。3 値（slept / awake / unknown） | 自動、Win-CI（記録を読めること） |
| 同 | 読めないときに 0 秒（＝スリープなし） | 共通化 | `unknown` として記録し、既定で保留にする | 自動 |
| 同 | WSL・Podman の仮想環境だけの停止 | 未対応 | ホストの電源の記録に現れない。検出していない | — |
| `src/aar/workspace.py` | `core.hooksPath=/dev/null` | 共通化 | `os.devnull`。あわせて `core.autocrlf=false`、`core.quotepath=false`、`core.longpaths=true` | 自動、Win-CI |
| 同 | `/Users/<名前>/` だけの検出 | 移植 | Windows（`C:\Users\…`、`/` や二重の `\\` も）、WSL（`/mnt/c/Users/…`、`\\wsl.localhost\…`）、UNC、`/home/…` を追加 | 自動 |
| 同 | 文字コードと改行の既定値 | 共通化 | 基盤が書くファイルはすべて UTF-8・LF（`newline="\n"`）。テストで強制 | 自動、Win-CI |
| `src/aar/audit/research.py` | `venv/bin/python` | 共通化 | Windows は `Scripts/python.exe` | 実物の監査では未確認 |
| 同 | `core.hooksPath=/dev/null`、`git clone` の改行 | 共通化 | `GIT_SAFE` を使う。複製で改行を変換しない | 自動 |
| `src/aar/audit/publication.py` | 文字コードの既定値 | 共通化 | UTF-8・LF を明示 | 自動 |
| `src/aar/publish/deliverables.py` | 公開用の写しでの `/Users/…` の置換 | 移植 | `workspace.redact_local_paths`（上の全種類）。**監査の原本には使わない**（写した側だけ） | 自動 |
| `src/aar/engine/ai_scientist.py` | `os.killpg(…, 15)`、`start_new_session` | 共通化 | `aar.proc.run`。Windows では穏やかな終了がなく、Job ごと終了する | 自動（上流の実物では未確認） |
| 同 | 上流（AI Scientist v3）の Windows での動作 | 未対応 | 任意の機能。上流が Windows を想定しているかは調べていない。ホストでの実行は、従来どおり明示の許可が要る | — |
| `src/aar/improve/evalkit.py` | `sandbox-exec`、それ以外は別プロセス | 移植 | Podman の経路（マウントなし・標準入力だけ）。計画が求める隔離の既定は `podman` | 自動、Podman-CI |
| 同 | `seatbelt` | 旧環境専用 | macOS だけ。新しい計画では採否に使えない | 自動（macOS） |
| 同 | `preexec_fn`、`resource`、固定の `PATH` | 共通化（テスト用の経路） | `process` の経路は POSIX でだけ資源の上限を付ける。採否には使えない | 自動、Win-CI |
| `src/aar/improve/loop.py` | 候補を作る側は、OS による制限なし | 未対応 | **生成側の隔離は未完成**。既定で生成を拒否し、人が明示したときだけ動かして記録する | 自動 |
| 同 | `chmod(0o600)` | 旧環境専用の効果 | Windows では実質的に働かない。封印評価の seed の保護は、置き場所のアクセス権で行う（[setup.md](setup.md)） | 実機 |
| `src/aar/store.py` | `osascript`（通知センター） | 旧環境専用 | macOS だけ。ほかの OS では、通知は DB に保存され、CLI と UI で見る | 自動 |
| `scripts/aarctl` | Bash、`nohup`、`kill`、`docker compose`、`caffeinate` | 旧環境専用 | 変更していない | — |
| `scripts/aarctl.ps1`、`aar ctl` | — | 移植 | PowerShell の入口と、OS に依らない制御（多重起動、古い PID、ログ、ワーカーの入れ替え）。WSL 方式では `wsl.exe` 経由で同じ処理を呼ぶ | 自動、Win-CI（status）。start は実機 |
| `infra/docker-compose.yml` | Docker Compose | 旧環境専用 | 変更していない。新しい環境は `podman run` を直接呼ぶ | Podman-CI |
| `src/aar/config.py` | — | 移植 | Podman・DB の役割・タイムゾーンなどの設定を追加。**方針ファイルは変更していない** | 自動 |
| `aar doctor` | Docker と `sandbox-exec` の確認 | 移植 | エンジンに応じた確認（Podman の構成、隔離の検証の有無、DB の権限、電源の記録、UTF-8 モード） | 実機 |
| `.github/workflows/ci.yml` | Linux だけ。PostgreSQL は Docker の service | 移植 | 3 経路に分けた（単体、Windows、Podman） | CI |
| `scripts/hooks/pre-push`、`scripts/install_skills.sh` | Bash | 未対応 | Windows では Git for Windows の Bash で動く見込みだが、確かめていない | 実機 |
| シンボリックリンク、ジャンクション | — | 該当なし | 基盤はシンボリックリンクを作らない。`git clone --no-hardlinks` はそのまま | — |
| 大文字と小文字 | — | 未確認 | 研究のリポジトリに、大文字小文字だけが違うファイル名があると、Windows では衝突する。既存の研究にあるかは、移送の前に確かめる（[migration.md](migration.md)） | 実機 |
| 長いパス | — | 一部 | Git には `core.longpaths=true` を渡す。Windows 側の長いパスの許可は社内の設定による | 実機 |

## 過去の記録の扱い

監査の記録、実験の記録、`docs/STATUS.md` の過去の節、`docs/improvement/2026-11-25-first-cycle.json` にある
「Docker」「macOS」「seatbelt」「`/Users/…`」という記述は、当時の事実なので書き換えていない。
