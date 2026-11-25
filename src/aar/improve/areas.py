"""継続して調べる領域と、外部の資料の登録内容。

領域は「調べる対象」として登録するだけで、常時実行はしない。どの領域も、既存のどの問題に効くという仮説なのか、何と比べるのか、
必要な資源、採用しない条件を持つ。別の環境やベンチマークでの改善は、この基盤での改善の証拠にしない。

資料の read_level は、実際に読んだ範囲を表す: listed（存在を確かめただけ）、abstract（要旨だけ）、partial（一部）、fulltext（本文）。
要旨しか読んでいないものを、理解・再現済みとして扱わない。
"""

from __future__ import annotations

from typing import Any

import psycopg

RETRIEVED_ON = "2026-11-25"

AREAS: list[dict[str, str]] = [
    {"key": "self_improving_harness", "title": "自己改善型ハーネス",
     "target_problem": "修正担当への文脈の取り落とし、監査の指示の不備など、基盤側の問題が実行の記録から見つかっても、直すのは人の作業になっている",
     "hypothesis": "実行の記録から弱点を拾い、小さな変更を隔離して評価する循環を回せば、基盤側の問題を直す手間が減る",
     "comparison": "現行の基盤（同じ課題、同じ予算）。変更なしを基準にする",
     "resources": "評価はローカルで決定的に行う。候補の生成に実行担当の呼び出しが 1 候補あたり 1 回",
     "reject_conditions": "評価器・採否の基準・保護領域に触れる変更。封印評価で基準に届かない変更。標本が足りないうちは採用しない"},
    {"key": "autonomous_science", "title": "自律型科学研究",
     "target_problem": "研究の型が「事前登録した仮説の検証」に限られ、評価関数の点数を上げる探索型の研究を扱えない",
     "hypothesis": "探索型のエンジン（AI Scientist v3）を、事前登録・監査の外側の工程として接続できる",
     "comparison": "現行の native エンジンでの同じ課題。監査の結果と費用で比べる",
     "resources": "数 GB の Docker イメージ。実行担当の利用枠（上限の機能が上流にない）",
     "reject_conditions": "全権限のエージェントをホストで動かす構成。回数と時間の上限を外から強制できない構成"},
    {"key": "evolutionary_search", "title": "進化的探索",
     "target_problem": "候補を 1 つずつ作って評価しており、複数の変種から選ぶ仕組みがない",
     "hypothesis": "評価関数が決定的で安い対象では、変種を複数作って選ぶと、単発の候補より良い解に届く",
     "comparison": "同じ総予算（生成の回数）での単発の候補",
     "resources": "生成の回数に比例する利用枠。決定的な評価器",
     "reject_conditions": "評価器への過適合を封印評価で確かめられない対象。複数の候補から最良を選んだ影響を報告できない場合"},
    {"key": "agent_memory", "title": "エージェント記憶",
     "target_problem": "過去の研究の教訓（lessons_learned）と観察は、埋め込みの近さで引くだけで、要約や関連づけをしていない",
     "hypothesis": "教訓を根拠つきで整理して引けば、同じ失敗の繰り返しが減る",
     "comparison": "現行の hybrid_search（全文検索＋埋め込み）での同じ問い合わせ",
     "resources": "ローカルの埋め込みモデルと pgvector。追加のサービスは使わない",
     "reject_conditions": "原記録を上書きする方式。要約に、根拠へのリンクと版が残らない方式"},
    {"key": "graph_knowledge", "title": "グラフによる知識整理・推論支援",
     "target_problem": "論文の引用関係（citations）は保存しているが、企画や証拠の検索に使っていない",
     "hypothesis": "引用関係をたどると、埋め込みの近さだけでは拾えない先行研究を、企画の段階で拾える",
     "comparison": "現行の検索（引用関係を使わない）での、同じ論文に対する先行研究の拾い漏れ",
     "resources": "既存の PostgreSQL の表（新しいグラフ DB は入れない）",
     "reject_conditions": "辺に根拠と関係の種類がない方式。モデルが推測した関係と確認済みの関係を区別しない方式。検索の成績を推論の正しさと同一視する評価"},
    {"key": "multi_agent", "title": "マルチエージェント協調",
     "target_problem": "研究の担当と監査の担当は分けているが、1 つの工程の中は単一のエージェントで進めている",
     "hypothesis": "独立性の高い調査の工程に限れば、並列の下請けが単一より良い",
     "comparison": "同じ総予算（トークン・呼び出し回数）の単一エージェント",
     "resources": "利用枠が数倍。8 GB のメモリでは並列の数に上限がある",
     "reject_conditions": "同じ総予算での比較がない場合。共有する状態が多い工程（実装、修正）への適用"},
    {"key": "test_time_compute", "title": "推論時の計算予算配分",
     "target_problem": "工程ごとのモデルは固定で、課題の難しさに応じて変えていない",
     "hypothesis": "難しさに応じてモデルと推論の強さを変えると、同じ利用枠で通る工程が増える",
     "comparison": "現行の固定の割り当て（config/policy.yaml）。品質ゲートの通過と利用量の両方で比べる",
     "resources": "工程ごとの利用量の記録（費用台帳にある）",
     "reject_conditions": "監査の担当や品質ゲートの判定を弱いモデルに下げる変更。利用上限・予算の上限に触れる変更"},
    {"key": "auto_curriculum", "title": "自動カリキュラム・研究課題生成",
     "target_problem": "評価の課題は、過去の実行の記録と手で書いた合成課題に限られる",
     "hypothesis": "失敗例から課題を自動で作れば、評価の課題が増え、標本不足で保留になる候補が減る",
     "comparison": "現行の課題の集まりでの、同じ候補の判定",
     "resources": "決定的に正解を検査できる課題の生成器",
     "reject_conditions": "正答や評価可能性を検査できない課題。易しい課題だけを増やして成績を上げる生成"},
    {"key": "agent_evaluation", "title": "エージェント評価",
     "target_problem": "基盤の変更の効果を、監査の点数や個別の実行の印象でしか語れない",
     "hypothesis": "決定的な採点と分割した課題で比べれば、変更の効果と後退を、変更の前に判定できる",
     "comparison": "評価なしで変更を入れる現行の進め方",
     "resources": "ローカルで完結する（モデルを呼ばない採点を優先する）",
     "reject_conditions": "監査の点数・テストの合格数・PR の数だけで研究能力の向上を主張する評価"},
    {"key": "durable_execution_safety", "title": "耐久実行・実行時安全性",
     "target_problem": "スリープからの復帰、スリープ中の失敗のやり直し、事前登録の変更による保留が、実物では未確認",
     "hypothesis": "未確認の経路を、検証用の複製で 1 つずつ実物で通せば、長期の無人運転を判断できる",
     "comparison": "自動テスト（スリープの記録の差し替えなど）だけで確かめた現状",
     "resources": "人の操作（復帰）、電源、検証用の複製 1 件",
     "reject_conditions": "本番の研究を使った試験。電源設定の変更を伴う試験を、ほかの作業に便乗して行うこと"},
]

# 外部の資料。版と到達可否は 2026-11-25 に確かめた。arXiv は要旨のページだけを読んだ（本文は読んでいない）
SOURCES: list[dict[str, str]] = [
    {"area_key": "self_improving_harness", "url": "https://arxiv.org/abs/2606.09498", "kind": "paper", "version": "v3",
     "title": "Self-Harness: Harnesses That Improve Themselves", "read_level": "abstract"},
    {"area_key": "self_improving_harness", "url": "https://arxiv.org/abs/2505.22954", "kind": "paper", "version": "v3",
     "title": "Darwin Godel Machine: Open-Ended Evolution of Self-Improving Agents", "read_level": "abstract"},
    {"area_key": "evolutionary_search", "url": "https://arxiv.org/abs/2506.13131", "kind": "paper", "version": "v1",
     "title": "AlphaEvolve: A coding agent for scientific and algorithmic discovery", "read_level": "abstract"},
    {"area_key": "autonomous_science", "url": "https://github.com/SakanaAI/AI-Scientist-v3", "kind": "repository", "version": "f8ae47d",
     "title": "AI-Scientist-v3 (The Agentic Scientist)", "read_level": "partial", "code_commit": "f8ae47d",
     "note": "2026-11-25 の時点で、既定のブランチの先頭は固定しているコミットと同じ。コードを読んだだけで、探索は実行していない（docs/research/ai-scientist-v3.md）"},
    {"area_key": "agent_evaluation", "url": "https://www.anthropic.com/research/demystifying-evals-for-ai-agents", "kind": "article",
     "version": "2026-01-09", "title": "Demystifying evals for AI agents", "read_level": "partial", "note": "冒頭の段落だけを確かめた"},
    {"area_key": "agent_memory", "url": "https://arxiv.org/abs/2502.12110", "kind": "paper", "version": "v11",
     "title": "A-MEM: Agentic Memory for LLM Agents", "read_level": "abstract"},
    {"area_key": "graph_knowledge", "url": "https://arxiv.org/abs/2404.16130", "kind": "paper", "version": "v2",
     "title": "From Local to Global: A Graph RAG Approach to Query-Focused Summarization", "read_level": "abstract"},
    {"area_key": "multi_agent", "url": "https://research.google/blog/towards-a-science-of-scaling-agent-systems-when-and-why-agent-systems-work/",
     "kind": "article", "version": "2026-01-28", "title": "Towards a science of scaling agent systems: When and why agent systems work",
     "read_level": "partial", "note": "冒頭の段落だけを確かめた"},
    {"area_key": "test_time_compute", "url": "https://arxiv.org/abs/2408.03314", "kind": "paper", "version": "v1",
     "title": "Scaling LLM Test-Time Compute Optimally can be More Effective than Scaling Model Parameters", "read_level": "abstract"},
    {"area_key": "auto_curriculum", "url": "https://arxiv.org/abs/2305.16291", "kind": "paper", "version": "v2",
     "title": "Voyager: An Open-Ended Embodied Agent with Large Language Models", "read_level": "abstract"},
    {"area_key": "durable_execution_safety", "url": "https://docs.temporal.io/develop/python/workflows/versioning", "kind": "documentation",
     "version": "", "title": "Versioning (Temporal Python SDK)", "read_level": "partial",
     "note": "patched / deprecate_patch の手順、Worker Versioning、再生テストの項を確かめた。ページに日付の表示はない"},
    {"area_key": "durable_execution_safety", "url": "https://code.claude.com/docs/en/sandboxing", "kind": "documentation", "version": "",
     "title": "Configure the sandboxed Bash tool", "read_level": "partial",
     "note": "Bash の隔離（macOS は Seatbelt、Linux は bubblewrap）の項を確かめた。末尾の一部は読んでいない。Read・Edit などの組み込みの道具は隔離の対象外と書かれている"},
]


def sync(conn: psycopg.Connection) -> dict[str, Any]:
    """領域と資料を台帳に入れる（同じ内容なら何もしない）。"""
    for a in AREAS:
        conn.execute("""INSERT INTO improvement_areas (key, title, target_problem, hypothesis, comparison, resources, reject_conditions)
                        VALUES (%(key)s, %(title)s, %(target_problem)s, %(hypothesis)s, %(comparison)s, %(resources)s, %(reject_conditions)s)
                        ON CONFLICT (key) DO UPDATE SET title = EXCLUDED.title, target_problem = EXCLUDED.target_problem,
                          hypothesis = EXCLUDED.hypothesis, comparison = EXCLUDED.comparison, resources = EXCLUDED.resources,
                          reject_conditions = EXCLUDED.reject_conditions, updated_at = now()""", a)
    for s in SOURCES:
        conn.execute("""INSERT INTO improvement_sources (area_key, url, kind, title, version, retrieved_on, read_level, code_commit, note)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (url, version) DO UPDATE SET title = EXCLUDED.title, read_level = EXCLUDED.read_level,
                          retrieved_on = EXCLUDED.retrieved_on, note = EXCLUDED.note""",
                     (s["area_key"], s["url"], s["kind"], s["title"], s["version"], RETRIEVED_ON, s["read_level"],
                      s.get("code_commit", ""), s.get("note", "")))
    return {"areas": len(AREAS), "sources": len(SOURCES)}
