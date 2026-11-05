"""Write heldout.json: new questions over the frozen S2 corpus, with gold, for the answerability step.

The corpus, scopes and principals are NOT copied: they are read from the frozen S2 dataset
(preparation/s2/evaluation/dataset.json, hash checked against its freeze.json) so both sets share one
corpus. Gold source hashes are taken from that corpus; a gold reference that does not exist, is not the
current version, or is not readable by the question's principal stops the script.

    python preparation/s3/answerability/write_heldout.py   # refuses to overwrite an existing heldout.json
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
S2 = PROJECT / "preparation/s2/evaluation"
sys.path.insert(0, str(PROJECT / "src"))

from forgeflow.contracts import Principal, Scope  # noqa: E402

# (id, principal, scope, tags, query, gold [(doc id, version)], counterpart)
# Answerable: phrased away from the frozen 47 (paraphrase, synonyms, Chinese <-> English).
# Unanswerable: each sits next to a real document on the same topic but asks for a detail it does not give.
# ACL pairs: the same query as an answerable one, asked by a principal who may not read the gold source.
QUESTIONS = [
    ("H01", "hw-a", "aurora-a", ["answerable", "english", "paraphrase"],
     "How many power-on cycles does EVT require at minimum?", [("evt-boot", "v1")], None),
    ("H02", "hw-a", "aurora-a", ["answerable", "chinese", "cross-language"],
     "EVT 阶段至少要完成多少次开机循环？", [("evt-boot", "v1")], None),
    ("H03", "hw-a", "aurora-a", ["answerable", "english", "paraphrase"],
     "What is the highest temperature allowed during EVT?", [("thermal-budget", "v1")], None),
    ("H04", "hw-a", "aurora-a", ["answerable", "chinese", "cross-language", "version"],
     "现在有效的物料清单是哪个版本？", [("npi-bom", "v2")], None),
    ("H05", "hw-a", "aurora-a", ["answerable", "english", "paraphrase"],
     "What happens if build validation in continuous integration fails?", [("ci-smoke", "v1")], None),
    ("H06", "hw-a", "aurora-a", ["answerable", "chinese", "cross-language"],
     "如何把一次部署回退到之前的版本？", [("sre-rollback", "v1")], None),
    ("H07", "hw-a", "aurora-a", ["answerable", "english", "paraphrase"],
     "What must be checked before writing a firmware image to the device?", [("android-flashing", "v1")], None),
    ("H08", "hw-a", "aurora-a", ["answerable", "english", "paraphrase"],
     "Does finishing an EVT run by itself move the project to the next stage?", [("evt-entry", "v1")], None),
    ("H09", "pm-a", "aurora-a", ["answerable", "chinese", "cross-language"],
     "技术检查通过和人工批准是一回事吗？", [("approval-rules", "v1"), ("zh-release", "v1")], None),
    ("H10", "hw-a", "aurora-a", ["answerable", "english", "paraphrase"],
     "When does a memory entry count as a confirmed decision?", [("memory-policy", "v1")], None),
    ("H11", "hw-a", "aurora-a", ["answerable", "english", "cross-language"],
     "What items does the release checklist require verifying?", [("zh-release", "v1")], None),
    ("H12", "hw-a", "aurora-a", ["answerable", "english", "cross-language"],
     "What has to be recorded during repeated power-cycling aging tests?", [("zh-aging", "v1")], None),
    ("H13", "pm-a", "aurora-a", ["answerable", "english", "cross-language"],
     "What should a project schedule list?", [("pm-schedule", "v1")], None),
    ("H14", "hw-a", "aurora-a", ["answerable", "english", "cross-language"],
     "Which checks happen before anyone can read the documents?", [("zh-permissions", "v1")], None),
    ("H15", "hw-a", "aurora-a", ["answerable", "chinese", "cross-language", "version"],
     "热测试报告当前版本记录的峰值温度是多少？", [("thermal-report-2026", "v2")], None),
    ("H16", "hw-high", "aurora-a", ["answerable", "english", "paraphrase", "classification"],
     "How much does the component cost in the restricted supplier table?", [("restricted-cost", "v1")], "H32"),
    ("H17", "hw-a", "aurora-a", ["answerable", "english", "paraphrase", "expiry"],
     "Can the archived RF chamber measurement be used as current evidence?", [("rf-archive", "v1")], None),
    ("H18", "pm-a", "aurora-a", ["answerable", "chinese", "cross-language", "role"],
     "供应商风险有什么缓解措施？", [("pm-risk", "v1")], "H31"),
    ("H19", "hw-a", "aurora-a", ["unanswerable", "english", "near-topic"],
     "What is the maximum allowed humidity during EVT?", [], None),
    ("H20", "hw-a", "aurora-a", ["unanswerable", "english", "near-topic"],
     "How many battery charge cycles must EVT pass?", [], None),
    ("H21", "hw-a", "aurora-a", ["unanswerable", "english", "near-topic"],
     "Who is allowed to sign the rollback bundle?", [], None),
    ("H22", "hw-a", "aurora-a", ["unanswerable", "english", "near-topic"],
     "Which BOM revision came before A2?", [], None),
    ("H23", "hw-a", "aurora-a", ["unanswerable", "english", "near-topic"],
     "What thermal peak did build B8 reach?", [], None),
    ("H24", "hw-a", "aurora-a", ["unanswerable", "chinese", "near-topic"],
     "EVT 跌落测试的高度要求是多少？", [], None),
    ("H25", "hw-a", "aurora-a", ["unanswerable", "english", "near-topic"],
     "How long does an Android CI smoke run take?", [], None),
    ("H26", "pm-a", "aurora-a", ["unanswerable", "chinese", "near-topic"],
     "发布审核需要几个人签字？", [], None),
    ("H27", "pm-a", "aurora-a", ["unanswerable", "english", "near-topic"],
     "Which supplier is the alternate source in the supplier risk plan?", [], None),
    ("H28", "hw-a", "aurora-a", ["unanswerable", "chinese", "near-topic"],
     "老化测试需要持续多少小时？", [], None),
    ("H29", "hw-a", "aurora-a", ["unanswerable", "english", "near-topic"],
     "What fastboot command unlocks the bootloader?", [], None),
    ("H30", "hw-a", "aurora-a", ["unanswerable", "english", "near-topic"],
     "Which reviewer approved build B7 for DVT?", [], None),
    ("H31", "hw-a", "aurora-a", ["acl-negative", "chinese", "role"],
     "供应商风险有什么缓解措施？", [], "H18"),
    ("H32", "hw-a", "aurora-a", ["acl-negative", "english", "classification"],
     "How much does the component cost in the restricted supplier table?", [], "H16"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build() -> dict:
    freeze = json.loads((S2 / "freeze.json").read_text())
    if sha256(S2 / "dataset.json") != freeze["files"]["dataset.json"]:
        raise SystemExit("frozen S2 dataset changed")
    corpus = json.loads((S2 / "dataset.json").read_text())
    docs = {(d["scope"], d["id"], d["version"]): d for d in corpus["documents"]}
    current = {}
    for d in corpus["documents"]:
        current[(d["scope"], d["id"])] = d["version"]  # versions are listed oldest first in the corpus
    questions = []
    for qid, principal, scope, tags, query, gold, counterpart in QUESTIONS:
        row = corpus["principals"][principal]
        who = Principal(subject_id="heldout-" + principal, kind="agent", roles=frozenset(row["roles"]),
                        scopes=(Scope.model_validate(corpus["scopes"][row["scope"]]),), clearance=row["clearance"])
        relevant = []
        for doc_id, version in gold:
            doc = docs[(scope, doc_id, version)]
            if current[(scope, doc_id)] != version:
                raise SystemExit(f"{qid}: gold {doc_id} {version} is not the current version")
            if not who.permits(Scope.model_validate(corpus["scopes"][scope]), set(doc["roles"]), doc["classification"]):
                raise SystemExit(f"{qid}: gold {doc_id} is not readable by {principal}")
            relevant.append({"scope": scope, "id": doc_id, "version": version, "source_hash": doc["source_hash"],
                             "status": "EXPIRED" if doc["valid_until"] else "CURRENT"})
        if ("answerable" in tags) != bool(relevant):
            raise SystemExit(f"{qid}: answerable questions need gold, others must have none")
        questions.append({"id": qid, "query": query, "principal": principal, "scope": scope, "tags": tags,
                          "relevant": relevant, "expected_denial": False, "conflict_required": False,
                          "counterpart": counterpart})
    return {
        "id": "aurora-answerability-heldout-v1",
        "corpus": {"path": "preparation/s2/evaluation/dataset.json", "sha256": freeze["files"]["dataset.json"],
                   "dataset_id": corpus["id"]},
        "authoring": "Written by the same author as the answerability code, before any model call on the corpus. "
                     "Gold assigned from the document text alone, without running retrieval or the model.",
        "relevance_policy": "Answerable: the gold document states the asked fact, rule or procedure; any one gold "
                            "source in the final result counts. Unanswerable: no document readable by the principal "
                            "states it, although a document on the same topic exists. ACL-negative: the only "
                            "answering document is unreadable by the principal, so the correct result is empty.",
        "top_k": 5,
        "questions": questions,
    }


if __name__ == "__main__":
    target = HERE / "heldout.json"
    if target.exists():
        raise SystemExit("heldout.json exists and is frozen; write a new versioned set instead")
    with target.open("x") as out:
        out.write(json.dumps(build(), ensure_ascii=False, indent=2) + "\n")
    print(target.relative_to(PROJECT), sha256(target))
