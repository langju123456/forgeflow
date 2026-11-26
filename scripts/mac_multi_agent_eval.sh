#!/bin/bash
# Four ways to answer the annual-report questions (single answer, self-check, executor + verifier, planner +
# executor + verifier) on a Mac with Ollama; commits and pushes the run. Meant to be sent through mac-bridge, from a
# clone under ~/code on the branch that carries preparation/s6/multi-agent:
#   bash scripts/mac_multi_agent_eval.sh            # development set, then the held-out set
#   bash scripts/mac_multi_agent_eval.sh dev        # development set only (the held-out set need not exist yet)
#   bash scripts/mac_multi_agent_eval.sh heldout    # held-out set only (it must be frozen and committed first)
#   bash scripts/mac_multi_agent_eval.sh draft      # no model: draft the held-out questions and the check list
#   bash scripts/mac_multi_agent_eval.sh draft MH03 # the same, replacing questions a reviewer rejected
#   bash scripts/mac_multi_agent_eval.sh freeze     # no model: freeze the set from the exported heldout-review.json
#   bash scripts/mac_multi_agent_eval.sh confirm-draft    # the same three for the confirmation set of revision 2
#   bash scripts/mac_multi_agent_eval.sh confirm-freeze   #   (confirm-registered.json; files confirm-*)
#   bash scripts/mac_multi_agent_eval.sh confirm          # confirmation set only, all four arms, run once
#
# TIME. One set of 40 questions is about 240 model calls, both sets about 470. The S4 run on this Mac made 105
# calls in 53 minutes (median 20.8 s, p95 54 s a call), so expect 1-2 hours a set and plan for 3.5. The queue's
# default timeout of 120 minutes is NOT enough: set it explicitly, 240 minutes for one set, 480 for both.
# A run that is cut off loses nothing: the same command continues it from the call records in its run folder
# (FORGEFLOW_NEW_RUN=1 starts over instead).
#
# PDFs come from ~/code/finreports (fetched and hash-checked here); embeddings (bge-m3) and every role's answers
# (forgeflow-qwen3-8b-ctx16k, the qwen3:8b weights with a 16k context) come from the local Ollama server; nothing
# leaves the machine.
set -euo pipefail
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}" no_proxy="localhost,127.0.0.1,::1"
cd "$(dirname "$0")/.."
MODE="${1:-all}"
case "$MODE" in all|dev|heldout|confirm|draft|freeze|confirm-draft|confirm-freeze) ;;
  *) echo "usage: $0 [dev|heldout|confirm|[confirm-]draft [ID...]|[confirm-]freeze]"; exit 64 ;; esac
[ $# -gt 0 ] && shift
SET=heldout; WHAT=held-out
case "$MODE" in confirm-*) SET=confirm; WHAT=confirmation; MODE="${MODE#confirm-}" ;; esac
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git pull -q --ff-only origin "$BRANCH" 2>/dev/null || true
echo "[arms] code at $(git log --oneline -1) on $BRANCH, mode $MODE"
S6=preparation/s6/multi-agent

py=""
for candidate in python3.12 /opt/homebrew/bin/python3.12 /opt/homebrew/opt/python@3.12/bin/python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then py="$(command -v "$candidate")"; break; fi
done
if [ -z "$py" ]; then
  echo "[arms] installing python@3.12 with Homebrew"; brew install -q python@3.12
  py=/opt/homebrew/opt/python@3.12/bin/python3.12
fi
echo "[arms] python: $("$py" --version)"
[ -x .venv-mac/bin/python ] || "$py" -m venv .venv-mac
.venv-mac/bin/pip install -q --upgrade pip
.venv-mac/bin/pip install -q -e '.[pdf]'

push() {  # commit what is staged, if anything, and push the current branch
  if git diff --cached --quiet; then echo "[arms] nothing new to commit"; return 0; fi
  git -c user.name="Lang Ju" -c user.email="langju123456@gmail.com" commit -q -m "$1"
  for attempt in 1 2 3; do
    git pull --rebase -q origin "$BRANCH" 2>/dev/null || true
    git push -q origin "$BRANCH" && break
    sleep 5
  done
  git log --oneline -1
}

if [ "$MODE" = freeze ]; then
  # The check list page downloads <set>-review.json into ~/Downloads; the newest one there is taken.
  review="$S6/$SET-review.json"
  newest="$(ls -t "$HOME"/Downloads/$SET-review*.json 2>/dev/null | head -1 || true)"
  if [ -n "$newest" ] && { [ ! -f "$review" ] || [ "$newest" -nt "$review" ]; }; then
    cp "$newest" "$review"; echo "[arms] review file: $newest"
  fi
  [ -f "$review" ] || { echo "[arms] no $SET-review.json: export it from $SET-checklist.html first"; exit 1; }
  status=0
  PYTHONDONTWRITEBYTECODE=1 .venv-mac/bin/python "$S6/write_heldout.py" freeze --set "$SET" --review "$review" \
    || status=$?
  git add "$S6/$SET-draft.json" "$review"
  if [ -f "$S6/$SET.json" ]; then
    git add "$S6/$SET.json" "$S6/$SET-freeze.json"
    push "S6 multi-agent: $WHAT questions checked against the printed pages and frozen before any run over them"
  else
    push "S6 multi-agent: review marks on the $WHAT draft recorded; some questions are still open, not frozen"
  fi
  exit $status
fi

REPORTS="$HOME/code/finreports"
bash scripts/fetch_real_reports.sh "$REPORTS"

if [ "$MODE" = draft ]; then
  # Page text only: no retrieval, no embedding, no model. Exit code 2 means too few candidates were found in the
  # reports; <set>-candidates.json says which, and it is committed either way.
  status=0
  exclude=()
  [ $# -gt 0 ] && exclude=(--exclude "$@")
  PYTHONDONTWRITEBYTECODE=1 .venv-mac/bin/python "$S6/write_heldout.py" draft --set "$SET" --reports "$REPORTS" \
    ${exclude[@]+"${exclude[@]}"} || status=$?
  git add "$S6/$SET-draft.json" "$S6/$SET-candidates.json" "$S6/$SET-checklist.md" 2>/dev/null || true
  push "S6 multi-agent: $WHAT questions drafted from the page text, every one still to be checked against the printed page"
  echo "[arms] check list to open on this Mac: $PWD/$S6/$SET-checklist.html"
  [ "${FORGEFLOW_OPEN_CHECKLIST:-0}" = 1 ] && open "$S6/$SET-checklist.html" || true
  exit $status
fi

curl -fsS --noproxy '*' http://127.0.0.1:11434/api/version >/dev/null \
  || { echo "[arms] starting ollama"; (ollama serve >/dev/null 2>&1 &); sleep 5; }
# A model already present is not pulled again: a registry update would change the weights the registration
# describes. Downloads through a desktop proxy sometimes drop ("unexpected EOF"); pull resumes.
for model in bge-m3 qwen3:8b; do
  if ollama show "$model" >/dev/null 2>&1; then continue; fi
  pulled=false
  for attempt in 1 2 3 4 5 6; do
    if ollama pull "$model" >/tmp/ollama-pull.log 2>&1; then pulled=true; break; fi
    echo "[arms] pull $model attempt $attempt failed: $(tail -c 200 /tmp/ollama-pull.log | tr '\r' '\n' | tail -1)"
    sleep 20
  done
  $pulled || { echo "[arms] could not pull $model"; exit 1; }
done
# Every role runs on the same weights under a local tag with num_ctx 16384: qwen3:8b itself loads with a
# 4096-token context here and silently truncates longer prompts (no download, no copy of the weights).
SERVED=forgeflow-qwen3-8b-ctx16k
if ! ollama show "$SERVED" >/dev/null 2>&1; then
  modelfile="$(mktemp)"
  printf 'FROM qwen3:8b\nPARAMETER num_ctx 16384\n' >"$modelfile"
  ollama create "$SERVED" -f "$modelfile"
  rm -f "$modelfile"
fi
echo "[arms] models:"; ollama list | grep -E "^(bge-m3|qwen3:8b|$SERVED)" || true

sets=(dev heldout)
[ "$MODE" = dev ] && sets=(dev)
[ "$MODE" = heldout ] && sets=(heldout)
[ "$MODE" = confirm ] && sets=(confirm)
resume=()
latest="$(ls -d "$S6"/runs/arms-* 2>/dev/null | grep -Ev -- '-(fake|plumbing)$' | tail -1 || true)"
if [ "${FORGEFLOW_NEW_RUN:-0}" != 1 ] && [ -n "$latest" ] && [ -f "$latest/calls.sqlite" ] && [ ! -f "$latest/report.json" ]; then
  echo "[arms] continuing the interrupted run in $latest"
  resume=(--resume "$latest")
fi
awake=()
command -v caffeinate >/dev/null 2>&1 && awake=(caffeinate -i)  # a sleeping laptop stalls a run of several hours
echo "[arms] sets: ${sets[*]}; expect 1-2 hours a set; the queue timeout must have been raised (240 min a set)"

# ${x[@]+"${x[@]}"}: macOS ships bash 3.2, where expanding an empty array under `set -u` is an error.
PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ${awake[@]+"${awake[@]}"} .venv-mac/bin/python "$S6/run_arms.py" \
  --reports "$REPORTS" --sets "${sets[@]}" ${resume[@]+"${resume[@]}"}

done_run="$(ls -d "$S6"/runs/arms-* | grep -Ev -- '-(fake|plumbing)$' | tail -1)"
git add "$done_run/report.json" "$done_run/run.json"
push "S6 multi-agent: single answer, self-check, executor + verifier, planner + verifier on ${sets[*]}, from the Mac"
