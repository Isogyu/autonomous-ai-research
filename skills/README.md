# 監査 Skill の置き場所

このフォルダには、利用者が別に管理している Skill を展開して置く。中身は Git に含めない。

| フォルダ | 役割 | 無い場合 |
|---|---|---|
| `independent-research-audit/` | 研究独立監査（必須） | 研究は監査の手前で保留になる |
| `github-publication-quality-audit/` | GitHub 公開品質監査・改善（任意） | 内蔵の確認項目で代用し、記録に「代用」と残す |

展開する:

```sh
scripts/install_skills.sh <研究独立監査の zip> [<公開品質監査の zip>]
```

監査担当（既定は Codex）には、Skill のフォルダの場所を渡して `SKILL.md` を読ませる。
`~/.codex/skills` や `~/.claude/skills` には何も入れない。
どの版の Skill で監査したかは、フォルダの内容のハッシュとして監査記録（`audit_runs.skill`）に残る。

公開品質監査 Skill をつなぐときの決まり（`src/aar/audit/publication.py`）:
監査担当は Skill の手順で監査し、結果を 7 項目の点数（合計 100 点）と指摘の一覧として返す。
Skill は採点と指摘だけを行い、修正は別の担当（修正担当）が行う。
