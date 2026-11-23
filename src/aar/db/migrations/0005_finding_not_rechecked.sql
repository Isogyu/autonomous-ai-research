-- 再監査で「解消」と言えるのは、再監査の担当に前回の指摘として渡したもの（Critical・Major）だけ。
-- 渡していない軽い指摘が次の回に挙がらなくても、解消したとは限らない（別の ID で挙がり直すことがある）。
-- それらは not_rechecked（追跡していない）として区別する。
ALTER TABLE audit_findings DROP CONSTRAINT audit_findings_status_check;
ALTER TABLE audit_findings ADD CONSTRAINT audit_findings_status_check
  CHECK (status IN ('open', 'fix_attempted', 'resolved', 'unresolved', 'not_rechecked'));
-- これまでの記録のうち、軽い指摘の「解消」は根拠がないので付け替える
UPDATE audit_findings SET status = 'not_rechecked', resolved_in_round = NULL
 WHERE status = 'resolved' AND severity IN ('Minor', 'Improvement');
