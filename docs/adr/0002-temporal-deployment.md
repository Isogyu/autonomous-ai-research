# ADR-0002: Temporal は単体サーバー（SQLite 永続化）で動かす

- 日付: 2026-11-23
- 状態: 採用（個人 PC 向け。常時稼働のサーバーに移すときは見直す）

## 状況

対象の PC はメモリ 8GB、ディスクの空きは 1GB 前後。Temporal の公式 Compose 構成
（`temporalio/samples-server` の PostgreSQL 版）は、サーバー・管理ツール・UI のコンテナを追加で必要とする。
`temporalio/auto-setup` は非推奨、`temporalio/docker-compose` はアーカイブ済み（2026-11-23 に確認）。

## 決定

`temporal server start-dev --db-filename var/temporal.db` で動かす（`scripts/aarctl`）。
履歴は SQLite ファイルに永続化され、プロセスや PC を再起動しても残る。
アプリ用の PostgreSQL とは共用しない。

## 理由

- 単一のバイナリで動き、Docker のメモリを使わない。Web UI（8233 番）も付く。
- 実測: 研究の途中でワーカーと Temporal を `kill -9` で止めて再起動し、同じ工程からやり直して完了した。
- 研究記憶（PostgreSQL）と進行の履歴（Temporal）を分けておくと、片方のスキーマ更新がもう片方に波及しない。

## 影響・限界

- 公式文書は start-dev を本番用途ではないとしている。単一プロセスなので冗長性はない。
- SQL 系の Visibility ではカスタム検索属性の数が少ないため、研究の検索は Temporal ではなく PostgreSQL で行う。
- 定期実行はタイムゾーンを `Asia/Tokyo` にし、PC が止まっていて逃した分は収集1日・企画3日までさかのぼって1回だけ実行する
  （既定の catchup は1年で、長く止めたあとにまとめて走るため）。
- テスト用の時間スキップサーバーは ARM に対応していないので、テストは開発用サーバー（`start_local`）で行う。
