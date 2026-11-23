# ライセンスの方針

- このリポジトリは非公開で、ライセンスは所有者がまだ決めていない。決まるまで LICENSE ファイルは置かない。
- 公開するときの第一候補は MIT（所有者の別の公開リポジトリと同じ）。特許条項が要るなら Apache-2.0。
- 依存しているもののライセンス: Temporal（MIT）、pgvector（PostgreSQL License）、AI Scientist v3（Apache-2.0、同梱しない）、
  multilingual-e5-small（MIT、同梱しない）。
- 監査 Skill は利用者の別の成果物で、このリポジトリには含めない（`skills/` の中身は Git の対象外）。
- 論文のメタデータ: arXiv は CC0。Semantic Scholar のデータは帰属表示が必要で、一部は非商用限定。OpenAlex は CC0。
  取得したメタデータは DB にだけ保存し、リポジトリには含めない。論文の本文は保存も再配布もしない。
- 研究ごとの成果物リポジトリ（`workspaces/<研究ID>/repo`）には、既定で MIT の LICENSE を生成する。
  著作権者の名前は Git の `user.name` を使う。変える場合は生成後に差し替える。
