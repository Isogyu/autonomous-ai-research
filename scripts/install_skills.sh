#!/usr/bin/env bash
# 利用者の監査 Skill を skills/ に展開する（skills/ の中身は Git に含めない）。
# 使い方: scripts/install_skills.sh <研究独立監査 Skill の zip> [<GitHub 公開品質監査 Skill の zip>]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ $# -ge 1 ] || { echo "使い方: $0 <independent-research-audit の zip> [<公開品質監査 Skill の zip>]"; exit 1; }
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
unzip -q "$1" -d "$tmp/a"
src="$(dirname "$(find "$tmp/a" -name SKILL.md | head -1)")"
[ -f "$src/SKILL.md" ] || { echo "zip に SKILL.md がない"; exit 1; }
[ -e "$ROOT/skills/independent-research-audit" ] && { echo "skills/independent-research-audit は既にある（上書きしない）"; } || cp -R "$src" "$ROOT/skills/independent-research-audit"
if [ $# -ge 2 ]; then
  unzip -q "$2" -d "$tmp/b"
  src="$(dirname "$(find "$tmp/b" -name SKILL.md | head -1)")"
  [ -e "$ROOT/skills/github-publication-quality-audit" ] && echo "既にある（上書きしない）" || cp -R "$src" "$ROOT/skills/github-publication-quality-audit"
fi
ls "$ROOT/skills"
