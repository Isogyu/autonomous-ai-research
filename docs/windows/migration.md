# Mac から Windows へ: バックアップ・復元・切替・切戻し

**この手順は計画で、まだ何も実行していない**（2026-11-25 時点）。本番の DB への適用、稼働環境の切替、データの移送は、
利用者の判断と、社内で許可された範囲の確認のあとに行う。Mac の原本、DB、実験結果、未追跡のファイルは削除しない。

## 原則

- Mac のコンテナの volume や仮想環境を、そのまま Windows にコピーしない。稼働中の SQLite のファイルだけをコピーして移行成功としない。
- 先に、新しい環境を検証用の DB・新しい名前空間・新しい保存先で確かめる（[setup.md](setup.md) の 6）。データを移すのは、そのあと。
- 秘密情報は移さない。`.env`、`~/.claude`、`~/.codex`、`gh` の認証、SSH の鍵は、社内の PC で新しく設定する。
- 移すのは、会社で許可された範囲だけ。**未追跡の `docs/research/AIエージェント最適化評価.md` は、コミットも移送もしない**（利用者が別に判断する）。
- 研究の内容の変更と、置き場所（ローカルのパス）の変更を区別して記録する。

## 1. 移す対象と、扱い

| 対象 | 場所（Mac） | 扱い |
|---|---|---|
| 基盤のリポジトリ | GitHub（非公開） | Windows で clone する。Mac の作業ツリーは残す |
| 研究の原本（RP-…7ee32c、RP-…72b244 など） | `workspaces/<id>/repo`（Git） | `git bundle` で移す。**内容は変更しない**。原本は Mac にも残す |
| 公開用・検証用の複製（PC-…、FX-…） | 同上 | 同上。提出済みの 2 件は GitHub の非公開リポジトリにもある |
| 監査の原本 | `workspaces/<id>/audits/` | ファイルとして移し、ハッシュを照合する。**置換しない** |
| 公開用の写し、最終検査の記録 | `repo/audit/`、`audits/final_check.json` | 同上。検査の対象のコミットの一致も確かめる |
| コミットの対応表 | `workspaces/PC-…/commit_map.tsv`（非公開） | ファイルとして移す。リポジトリには入れない |
| 研究の DB（`aar`） | PostgreSQL の volume | 論理バックアップ（`pg_dump`）と復元 |
| 改善の台帳（`aar_improve`） | 同じ PostgreSQL の別の DB | 同上。追記だけの制約と、操作主体の記録が保たれることを確かめる |
| 改善の評価の記録 | `var/improve/`（計画・課題・生の出力・seed）、`docs/improvement/2026-11-25-first-cycle.json` | ファイルとして移す。seed は本人だけが読める場所へ |
| Temporal の履歴 | `var/temporal.db`（SQLite） | **持ち越さない**（下の 4） |
| 論文の本文、埋め込みのモデル | `var/papers/`、`var/models/` | 再取得できる。移す必要はない（権利の確認が要るものを社内に持ち込まない） |
| ログ、バックアップ | `var/log/`、`var/backup-…` | Mac に残す（参照用） |

## 2. バックアップ（Mac。読み取りだけ）

前提: 実行中・保留中の研究がないこと（`aar projects list`）。ある場合は、終わるか、利用者が止めるまで待つ。勝手に止めない。

```bash
# 版を記録する（復元先と合わせるため）
docker exec aar-postgres psql -U aar -d aar -Atc "select version(); select extversion from pg_extension where extname='vector';"
# 論理バックアップ（独自形式。所有者と権限は復元先で付け直す）
docker exec aar-postgres pg_dump -U aar -d aar -Fc --no-owner --no-privileges > aar.dump
docker exec aar-postgres pg_dump -U aar -d aar_improve -Fc --no-owner --no-privileges > aar_improve.dump
shasum -a 256 aar.dump aar_improve.dump > db.sha256
# 行数の控え（復元のあとで照合する）
docker exec aar-postgres psql -U aar -d aar_improve -Atc "select 'runs', count(*) from improvement_runs union all select 'events', count(*) from improvement_events union all select 'decisions', count(*) from improvement_decisions union all select 'plans', count(*) from improvement_eval_plans union all select 'candidates', count(*) from improvement_candidates;"
```

研究のリポジトリと記録:

```bash
for id in $(ls workspaces); do
  git -C "workspaces/$id/repo" bundle create "$PWD/export/$id.bundle" --all
  git -C "workspaces/$id/repo" for-each-ref --format='%(refname) %(objectname)' > "export/$id.refs"
  git -C "workspaces/$id/repo" fsck --full
done
# 監査の原本、対応表、最終検査の記録、改善の評価の記録のハッシュ（相対パスで記録する）
find workspaces/*/audits workspaces/*/commit_map.tsv var/improve -type f -print0 | sort -z | xargs -0 shasum -a 256 > export/files.sha256
```

- 事前登録（`repo/prereg/`）と生データ（`repo/results/`）は Git の中にあるので、コミットの ID が一致すれば内容も一致する。
- 研究に固定した方針のハッシュ（`projects.policy_sha256`）と、いまの方針ファイルのハッシュ（`aar doctor`）を控える。
  このブランチは方針ファイルを変更していないので、同じはず。

## 3. 復元（Windows。検証が済んだ新しい環境へ）

1. 移送の前に、範囲が会社で許可されているかを確かめる。媒体と経路も、許可されたものだけを使う。
2. 受け取ったファイルのハッシュを照合する（`Get-FileHash -Algorithm SHA256`。`db.sha256`、`files.sha256` と一致）。
3. PostgreSQL と pgvector の版を、Mac の控えと比べる。同じイメージ（`pgvector/pgvector:0.8.7-pg17`）を使う。
   CPU の種類が違っても（Mac は arm64、Windows は多くが x86_64）、論理バックアップなら移せる。volume のコピーは移せない。
4. 復元する（所有者の接続で）。
   ```powershell
   podman exec -i aar-postgres createdb -U aar aar_improve
   Get-Content -AsByteStream aar.dump | podman exec -i aar-postgres pg_restore -U aar -d aar --no-owner --no-privileges --exit-on-error
   Get-Content -AsByteStream aar_improve.dump | podman exec -i aar-postgres pg_restore -U aar -d aar_improve --no-owner --no-privileges --exit-on-error
   ```
   PowerShell のパイプは、版によってバイナリを壊すことがある（Windows PowerShell 5.1）。確実なのは、`podman cp` でコンテナに
   ファイルを入れてから `pg_restore` を実行する方法。**どちらが使えるかは、実機で確かめる。**
5. 確かめる。
   - 行数が、Mac の控えと一致する。`schema_migrations` の内容が一致する（`aar migrate` が「なし（最新）」を返す）。
   - 追記だけの表のトリガーが有効（`select tgname, tgenabled from pg_trigger where tgname like 'improvement_%'` で `O`）。
   - 実行用の役割を作り（[setup.md](setup.md) の 4。DB ごとに実行する）、その役割で UPDATE・DELETE・トリガーの無効化が拒否される。
   - 操作主体の記録（`actor_kind`）が、復元の前後で同じ（human / claude_code / codex / scheduler / policy の件数）。
   - `aar improve show IC-20261124-6e4bf7` が「棄却」のまま。復元の操作は、台帳に新しい記録を足さない。
6. 研究のリポジトリを復元する。
   ```powershell
   git -c core.autocrlf=false clone <id>.bundle workspaces\<id>\repo
   git -C workspaces\<id>\repo for-each-ref --format='%(refname) %(objectname)'    # Mac の .refs と一致すること
   git -C workspaces\<id>\repo fsck --full
   git -C workspaces\<id>\repo status --porcelain                                  # 何も出ないこと（改行の変換が起きていない）
   ```
   大文字と小文字だけが違うファイル名がある研究は、Windows では取り出せない。`git ls-files | sort -f | uniq -di` で先に確かめる。
7. 監査の原本と対応表を置き、`files.sha256` と照合する。
8. `aar projects required-check <id>`（読み取りだけ）を、移した研究で実行する。Mac での結果と同じになること
   （ローカルのパスが変わっても、検査の結果と、検査の対象のコミットが変わらないこと）。
9. DB の中の絶対パス（作業領域の場所など）が Mac のもののまま残る。**書き換えない**（記録として残す）。
   新しい実行は新しい場所で始まる。旧い研究を Windows で再開する場合に何が要るかは、実機で 1 件（検証用の複製）で確かめてから決める。

## 4. Temporal の履歴

- 稼働しているのは開発用の単体サーバー（`temporal server start-dev`、SQLite）。公式の情報で確かめられたのは、ワークフローの
  コードを変えるときの互換性の保ち方（patching、再生のテスト）まで。**開発用サーバーの SQLite を別の PC・別の OS へ移す、
  公式に対応した手順は確認できていない**（未確認であって、不可能と確かめたわけではない）。
- したがって、履歴は持ち越さない。Mac の `var/temporal.db` は、参照用として Mac に残す。
- 現在、実行中・保留中の研究はない（`docs/HANDOFF.md`）。切替の時点でもそうであることを確かめる。
  終わっていないワークフローが残っていたら、切り替えない。完了扱いにもしない。
- Windows では新しい名前空間（`AAR_TEMPORAL_NAMESPACE=aar-win`）で始める。研究の ID は DB にあるので、新旧の対応は
  `projects.workflow_id` と、切替の記録（日時、Mac の最後のワークフロー、Windows の最初のワークフロー）で残す。
- 旧い研究を Windows で `reopen` すると、新しいワークフローとして始まる（履歴の再生はない）。このブランチでワークフローに足した
  分岐は `workflow.patched` で囲んであり、Mac の既存の履歴の再生には影響しない。
- 履歴を持ち越すことにした場合は、Temporal の版の互換性と、移した履歴の再生（Replayer）を先に確かめる。

## 5. 切替（Mac と Windows の両方で同じ処理を動かさない）

1. **判断**: 利用者が、切替の日時を決める。未完了の作業（`docs/HANDOFF.md` の「次にやること」）を、どちらで行うかも決める。
2. Mac: 実行中・保留中の研究がないことを確かめる。利用上限の待機がないことを確かめる（`aar limits`）。
3. Mac: 定期実行を止める（Temporal の UI で `aar-daily-ingest` と `aar-weekly-proposals` を一時停止）。
4. Mac: 最後のバックアップを取る（上の 2）。
5. Mac: `scripts/aarctl stop --all`。**利用者が行うか、利用者の了承を得てから行う。** データは消さない。
6. Windows: 復元して確かめる（上の 3）。
7. Windows: `.\scripts\aarctl.ps1 start`。定期実行が 1 組だけ登録されていることを、Temporal の UI で確かめる。
8. Windows: 検証用の複製 1 件で、一巡を確かめる。
9. 切替の記録を `docs/STATUS.md` に書く（日時、バックアップのハッシュ、行数、新旧のワークフローの対応）。

切替のあと、Mac のワーカーを起動しない。起動する必要が出たら、先に Windows 側を止める。

## 6. 切戻し

- Mac の環境は、切替の時点のまま残っている（volume、`var/`、`workspaces/`）。
- Windows 側で新しい研究や記録が**できていない**うちは、Windows を止めて（`stop --all`）、Mac で `scripts/aarctl start` すれば戻る。
- Windows 側で新しい記録ができたあとは、単純には戻せない。Windows で `pg_dump` を取り、Mac へ復元する（逆向きの手順）。
  その場合、Mac の DB は別名で残してから復元する。**上書きしない。**
- 旧環境でこのブランチのコードを動かす場合は、`.env` に `AAR_CONTAINER_ENGINE=docker` を明示する（[ADR-0012](../adr/0012-windows-podman.md) の 6）。
- 切り戻した場合も、切替と同じく、両方で同時に動かさない。

## 7. 切替の前に、利用者が判断すること

- 移してよいデータの範囲（会社の決まり）。研究の原本を社内の PC に置いてよいか。
- 実行方式（A / B）と、その根拠になる社内の回答。
- PR #11 とこの PR のマージの順序。移行 0006 を、どの DB に、いつ適用するか。
- 切替の日時と、Mac を止める操作を誰が行うか。
- Temporal の履歴を持ち越さないことでよいか。
- 修正した 2 件の複製の再監査（`docs/HANDOFF.md` の「次にやること」の 1）を、切替の前に Mac で行うか、後に Windows で行うか。
