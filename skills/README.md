# 監査 Skill の置き場所

このフォルダには、利用者が別に管理している Skill を展開して置く。中身は Git に含めない。

| フォルダ | 役割 | 無い場合 |
|---|---|---|
| `independent-research-audit/` | 研究独立監査 | 研究は監査の手前で保留になる |
| `repo-publication-audit/` | GitHub 公開品質監査 | 研究は公開品質監査の手前で保留になる（代用の監査では通さない） |

展開する:

```sh
scripts/install_skills.sh <研究独立監査の zip> <公開品質監査の zip>
```

zip の中の `SKILL.md` があるフォルダを、そのフォルダ名のまま `skills/` に写す。既にあれば上書きしない。

## 実行のしかた

監査担当（既定は Codex）に Skill のフォルダの場所を渡し、`SKILL.md` と参照資料を読ませて、その手順で監査させる。
`~/.codex/skills` や `~/.claude/skills` には何も入れず、スラッシュコマンドとしての呼び出しも使わない。
どの版の Skill で監査したかは、フォルダの内容のハッシュとして監査記録（`audit_runs.skill`）に残る。

### 公開品質監査（`repo-publication-audit`）

- モードは `audit`（読み取り専用）。監査担当は対象の複製に何も書き込めず、テストも実行しない。
  テストはオーケストレーターが隔離環境で実行し、結果を「既存のログ」として渡す。
- `improve` モードは使わない。指摘の修正は別の担当（修正担当）が行い、そのあと最初から監査し直す。
- 結果は Skill の採点基準（7項目・21内訳、確認済み得点と保留点）と報告形式のまま受け取り、
  決まりに合っているか検証してから保存する（`src/aar/audit/publication.py` の `validate_skill_result`）。
  合っていなければ正常な監査結果として扱わず、やり直す。3回やり直しても駄目なら研究は「失敗」で確定する。
- この Skill は Claude Code 用として配布されているが、ここでは実装担当（Claude Code）と別系統にするため Codex に実行させている。
  `config/policy.yaml` の `research.models.auditor` で変えられる。

### 内蔵の検査との関係

秘密情報、文章中の数値の由来、決まった節、ライセンス、外部データの記録、テストの合否は、Skill とは別に機械的に検査する。
結果は Skill に補助の資料として渡し、問題があれば Skill の採点にかかわらず指摘として残る。
内蔵の確認項目による LLM 監査は、`gates.publication.require_formal_skill: false` にしたときだけ使う代用で、記録に「代用」と残る。
