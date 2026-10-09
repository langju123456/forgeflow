#!/usr/bin/env bash
# Build the public copy of this repository's history.
#
#   scripts/publish_public_history.sh <new-empty-dir> [branch]
#
# The sources/ directory (personal planning material that is not part of the project) is removed from every
# commit, and the frozen-evidence anchor is re-pointed at the rewritten baseline commit, so the replay tests
# keep working. Everything else (files, messages, authors, dates) is unchanged and checked below. The working
# repository is never modified: the result is a fresh clone in <new-empty-dir>, plus an old -> new commit map
# in <new-empty-dir>.history-map.txt. The rewrite is deterministic, so publishing again after new commits
# gives the same ids for the old ones and the push is a fast-forward.
set -euo pipefail
src=$(git rev-parse --show-toplevel)
out=${1:?usage: publish_public_history.sh <new-empty-dir> [branch]}
branch=${2:-master}
out=${out%/}
map_file="$out.history-map.txt"
anchor_file=deliveries/2026-10-06/s2c-fixes/evidence/freeze-git-anchor.json
[ ! -e "$out" ] || { echo "refusing: $out already exists" >&2; exit 2; }
old=$(git -C "$src" show "$branch:$anchor_file" | python3 -c 'import json,sys; print(json.load(sys.stdin)["git_commit"])')
[ "$(git -C "$src" rev-parse "refs/tags/freeze-baseline^{commit}")" = "$old" ] \
  || { echo "refusing: tag freeze-baseline does not point at the anchor commit" >&2; exit 2; }

git clone --quiet --no-local --single-branch --branch "$branch" "$src" "$out"
cd "$out"
git fetch --quiet origin "refs/tags/freeze-baseline:refs/tags/freeze-baseline"
git remote remove origin
export anchor_file old
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch \
  --index-filter '
    git rm -r --cached --ignore-unmatch --quiet sources >/dev/null
    if git cat-file -e ":$anchor_file" 2>/dev/null; then
      new=$(map "$old"); [ "$new" != "$old" ] || exit 3
      blob=$(git cat-file blob ":$anchor_file" | sed "s/$old/$new/" | git hash-object -w --stdin)
      git update-index --cacheinfo "100644,$blob,$anchor_file"
    fi' \
  --tag-name-filter cat -- "$branch" freeze-baseline >/dev/null

# old -> new commit map, then drop the pre-rewrite refs and objects from this copy
paste -d' ' <(git rev-list --reverse --topo-order "refs/original/refs/heads/$branch") \
            <(git rev-list --reverse --topo-order "$branch") > "$map_file"
git for-each-ref --format='%(refname)' refs/original/ | while read -r ref; do git update-ref -d "$ref"; done
git reflog expire --expire=now --all && git gc --quiet --prune=now

# checks
fail() { echo "CHECK FAILED: $*" >&2; exit 1; }
root=$(git rev-list --max-parents=0 "$branch")
anchor=$(git show "$branch:$anchor_file" | python3 -c 'import json,sys; print(json.load(sys.stdin)["git_commit"])')
[ "$anchor" = "$root" ] && [ "$(git rev-parse 'freeze-baseline^{commit}')" = "$root" ] || fail "anchor, tag and root commit differ"
[ -z "$(git rev-list --objects --all | awk '$2 ~ /^sources(\/|$)/')" ] || fail "sources/ is still reachable"
keep() { awk -F'\t' -v f="$anchor_file" '$2 !~ /^sources\// && $2 != f'; }
while read -r o n; do
  [ "$(git -C "$src" log -1 --format='%an%ae%at%cn%ce%ct%B' "$o")" = "$(git log -1 --format='%an%ae%at%cn%ce%ct%B' "$n")" ] \
    || fail "metadata differs for $o"
  [ "$(git -C "$src" ls-tree -r "$o" | keep)" = "$(git ls-tree -r "$n" | keep)" ] \
    || fail "$o -> $n changes more than sources/ and the anchor file"
  if git -C "$src" cat-file -e "$o:$anchor_file" 2>/dev/null; then
    [ "$(git -C "$src" show "$o:$anchor_file" | sed "s/$old/$root/")" = "$(git show "$n:$anchor_file")" ] \
      || fail "the anchor file in $n is not the original with the new baseline id"
  fi
done < "$map_file"
echo "OK: $(wc -l < "$map_file" | tr -d ' ') commits rewritten; anchor $old -> $root; tip $(git rev-parse "$branch")"
