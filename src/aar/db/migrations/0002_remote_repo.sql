-- 研究ごとの push 先（owner/name）。空なら PR は作らず、公開候補はローカルのブランチに留める。
ALTER TABLE research_projects ADD COLUMN remote_repo text;
-- 研究開始時に検索した過去の記憶（どの記憶を設計に使ったかを後から追えるようにする）
ALTER TABLE research_projects ADD COLUMN memory_context text;

-- 研究記憶を種類をまたいで検索するための統一ビュー
CREATE VIEW memory_documents AS
  SELECT 'paper'::text AS entity_type, p.id::text AS entity_id, NULL::text AS project_id, p.title AS title,
         coalesce(p.abstract, '') AS body, p.tsv AS tsv, p.first_seen_at AS created_at,
         'external_fact'::text AS origin
    FROM papers p
  UNION ALL
  SELECT 'proposal', r.id::text, (SELECT id FROM research_projects WHERE proposal_id = r.id), r.title,
         coalesce(r.body->>'unknown', '') || ' ' || coalesce(r.body->>'method', '') || ' ' || coalesce(r.body->>'hypotheses', ''),
         r.tsv, r.created_at, r.origin::text
    FROM research_proposals r
  UNION ALL
  SELECT 'hypothesis', h.id::text, h.project_id, h.label || '（' || h.kind || '・' || h.status || '）',
         h.statement || ' ' || coalesce(h.decision_rule, ''), h.tsv, h.created_at, 'ai_inference'
    FROM hypotheses h
  UNION ALL
  SELECT 'experiment', e.id::text, e.project_id, e.name || '（' || e.kind || '・' || e.status || '）',
         coalesce(e.design, ''), e.tsv, coalesce(e.started_at, now()), 'measured'
    FROM experiments e
  UNION ALL
  SELECT 'finding', f.id::text, f.project_id, '[' || f.severity || '] ' || f.title,
         coalesce(f.detail::text, ''), f.tsv, f.created_at, 'ai_inference'
    FROM audit_findings f
  UNION ALL
  SELECT 'lesson', l.id::text, l.project_id, '[' || l.category || '] ' || left(l.content, 60),
         l.content, l.tsv, l.created_at, l.origin::text
    FROM lessons_learned l;
