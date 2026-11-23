# ADR-0004: 研究記憶は PostgreSQL + pgvector、SQL を直接書く

- 日付: 2026-11-23
- 状態: 採用

## 決定

- 永続化は PostgreSQL 17、意味検索は pgvector 0.8.7（HNSW、コサイン距離）。専用のベクトル DB は入れない。
- ORM を使わず psycopg 3 で SQL を書く。スキーマは番号つきの SQL ファイルで移行する（`aar/db/migrations`）。
  適用済みのファイルが書き換えられたら起動時にエラーにする。
- 検索はハイブリッド（`aar/memory/search.py`）: 全文検索と部分一致、ベクトル検索、種類や研究での絞り込み、引用関係。
  順位の統合は Reciprocal Rank Fusion（順位だけを使う）。
- 種類をまたぐ検索のため、論文・企画・仮説・実験・監査指摘・学びを1つのビュー（`memory_documents`）にまとめる。
- 知識には出どころ（`origin`: 外部資料の事実 / AI の推測 / 実測 / 利用者）を必ず付け、論文には情報源・取得日時・版を残す。

## 理由

- 研究件数は多くても数千件の規模で、pgvector で十分。DB を1つにすれば、絞り込みと結合を SQL で書ける。
- 検索の SQL は品質に直結するので、ORM の裏に隠さない。
- ベクトルの近さは「関連しそうな記録を拾う」ためだけに使う。新規性や正しさの判定には使わない
  （論文の重複判定も DOI・arXiv ID・題名と著者で行う）。

## 埋め込みモデル

- 採用は `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（384 次元、Apache-2.0、日本語と英語を含む 50 以上の言語、
  ONNX で約 0.24GB）。fastembed で CPU 実行する（`pip install '.[embed]'`）。モデルは `var/models` に置く。
- 当初の候補 `intfloat/multilingual-e5-small` は、fastembed の対応モデルに無かった（e5 系は large だけで、約 2.2GB あり
  メモリ 8GB の PC には重い）。PyTorch を入れずに済ませるため、同じ 384 次元の上記モデルにした。
- 動作の確認（2026-11-23）: 「エージェントの評価ベンチマーク」と "benchmark for evaluating LLM agents" のコサイン類似度は 0.53、
  無関係な文（灌水制御）とは 0.03。精度の比較評価はしていない。
- fastembed が導入されていない環境では、文字 n-gram のハッシュによる 384 次元ベクトルで代用する。
  **これは表記の近さしか捉えない。** 記録上は別のモデル名（`hash-ngram-384`）になり、検索は同じモデルどうしでだけ行う。
- Ruri v3 は日本語専用なので候補から外した。

## 限界

- 全文検索の設定は `simple`。日本語は空白で区切られないため、部分一致（`ILIKE`）で補っている。件数が増えたら pg_bigm などを検討する。
