#!/usr/bin/env bash
# 利用者の監査 Skill を skills/ に展開する（skills/ の中身は Git に含めない）。
# 使い方: scripts/install_skills.sh <Skill の zip>...
# zip の中で SKILL.md があるフォルダを、そのフォルダ名のまま skills/ に写す。既にあれば上書きしない。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ $# -ge 1 ] || { echo "使い方: $0 <Skill の zip>..."; exit 1; }
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
n=0
for zip in "$@"; do
  n=$((n + 1))
  unzip -q "$zip" -d "$tmp/$n"
  found="$(find "$tmp/$n" -name SKILL.md | head -1)"
  [ -n "$found" ] || { echo "$zip に SKILL.md がない"; exit 1; }
  src="$(dirname "$found")"; name="$(basename "$src")"
  if [ -e "$ROOT/skills/$name" ]; then echo "skills/$name は既にある（上書きしない）"; else cp -R "$src" "$ROOT/skills/$name"; echo "skills/$name を置いた"; fi
done
