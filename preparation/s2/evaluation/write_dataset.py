"""One-time manual synthetic fixture authoring; refuse replacement after freezing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATASET_PATH = ROOT / "dataset.json"
SCOPES = {
    "aurora-a": {"tenant_id": "eval-a", "workspace_id": "engineering", "project_id": "aurora"},
    "orion-a": {"tenant_id": "eval-a", "workspace_id": "engineering", "project_id": "orion"},
    "ops-a": {"tenant_id": "eval-a", "workspace_id": "operations", "project_id": "aurora"},
    "aurora-b": {"tenant_id": "eval-b", "workspace_id": "engineering", "project_id": "aurora"},
}
SHARED = ["hardware", "pm", "supervisor"]
DOCUMENTS = []


def doc(
    id, content, *, version="v1", roles=None, classification=0, valid_until=None, scope="aurora-a", note=""
):
    DOCUMENTS.append(
        {
            "scope": scope,
            "id": id,
            "version": version,
            "content": content,
            "source_hash": hashlib.sha256(content.encode()).hexdigest(),
            "roles": roles or ["hardware"],
            "classification": classification,
            "valid_until": valid_until,
            "design_note": note,
        }
    )


doc(
    "evt-boot",
    "EVT boot cycles minimum 100. Boot failures must be zero. Required evidence carries observed_at and valid_until.",
    roles=SHARED,
)
doc(
    "thermal-budget",
    "EVT thermal peak maximum 70 Celsius. A current laboratory measurement must identify the build.",
)
doc("npi-bom", "Bill of materials BOM revision A2 is obsolete for this synthetic project.", version="v1")
doc(
    "npi-bom",
    "Bill of materials BOM revision A3 is the current fixture. It supersedes earlier A2.",
    version="v2",
)
doc(
    "ci-smoke",
    "Android CI smoke must succeed. Failed build validation blocks the synthetic gate.",
    roles=["hardware", "sre"],
)
doc(
    "sre-rollback",
    "Rollback uses a signed rollback bundle. Pause the deployment, then restore the previous image.",
    roles=["hardware", "sre"],
)
doc(
    "android-flashing",
    "Use fastboot flash only with an approved firmware manifest. Verify the image signature before writing.",
    roles=["hardware", "android"],
)
doc(
    "evt-entry",
    "EVT entry follows PROTOTYPE. A completed run does not alone advance the project stage.",
    roles=SHARED,
)
doc(
    "approval-rules",
    "Human stage approval is separate from technical PASS. Approval binds immutable parameters, policy, source versions and stage revision.",
    roles=SHARED,
)
doc(
    "memory-policy",
    "Memory candidate is unconfirmed history. Confirmed decisions require a reviewer, scoped source events and unchanged classification.",
    roles=SHARED,
)
doc(
    "zh-release",
    "发布 检查清单：验证负责人、回滚材料、交付版本以及审核记录。技术检查与人工批准分别记录。",
    roles=SHARED,
)
doc("zh-aging", "老化 测试：重复上电并记录失败次数，标注测试样本、硬件批次和观测时间。")
doc("pm-schedule", "项目排期 应标注项目经理、责任人、里程碑与依赖关系；未解决风险必须记录。", roles=["pm"])
doc(
    "zh-permissions",
    "权限 检查：先验证租户、工作区、项目、角色与密级，再读取资料；猜测标识不能获得访问。",
    roles=SHARED,
)
doc(
    "thermal-report-2026",
    "THERMAL-REPORT-2026 reported thermal peak 82 Celsius for the old build.",
    version="v1",
)
doc(
    "thermal-report-2026",
    "THERMAL-REPORT-2026 current revision reports thermal peak 68 Celsius for build B7.",
    version="v2",
)
doc(
    "labnote-boot-pass",
    "Labnote current boot measurement: Aurora completed 100 cycles with zero failures. The subject is build B7 boot count.",
    note="Unresolved contradictory source pair with labnote-boot-fail; real Store has no cross-document subject_key.",
)
doc(
    "labnote-boot-fail",
    "Labnote current boot measurement: Aurora completed 60 cycles with zero failures. The subject is build B7 boot count.",
    note="Unresolved contradictory source pair with labnote-boot-pass; neither record is superseded.",
)
doc(
    "rf-archive",
    "Radio-frequency RF chamber report uses obsolete calibration. This archived chamber measurement is not current evidence.",
    valid_until="2000-01-01T00:00:00Z",
    note="Expired exact citation can be structurally valid but is not current evidence.",
)
doc(
    "pm-risk",
    "Supplier risk mitigation uses a contingency schedule and an alternate source. PM-only synthetic planning note.",
    roles=["pm"],
)
doc(
    "restricted-cost",
    "Restricted synthetic supplier cost table: component price is 42 units. This is a fake classroom record.",
    classification=2,
)
doc(
    "secret-decoy",
    "EVT entry EVT entry EVT entry EVT entry. Forbidden high-classification decoy; never put this synthetic record in low-clearance context.",
    classification=3,
    note="Stronger lexical match than evt-entry, tests ACL before ranking.",
)
doc(
    "private-canary",
    "NEVER-APPROVED-P1 synthetic token belongs to a PM-only note. It grants no tool or approval authority.",
    roles=["pm"],
)
for alternate in ["orion-a", "ops-a", "aurora-b"]:
    doc(
        "evt-boot",
        DOCUMENTS[0]["content"],
        roles=SHARED,
        scope=alternate,
        note="Same content and ID in another full scope; explicit repository metadata trace checks source scope.",
    )

PRINCIPALS = {
    "hw-a": {"scope": "aurora-a", "roles": ["hardware"], "clearance": 1},
    "hw-low": {"scope": "aurora-a", "roles": ["hardware"], "clearance": 0},
    "hw-high": {"scope": "aurora-a", "roles": ["hardware"], "clearance": 3},
    "pm-a": {"scope": "aurora-a", "roles": ["pm"], "clearance": 0},
    "hw-b": {"scope": "aurora-b", "roles": ["hardware"], "clearance": 1},
    "hw-orion": {"scope": "orion-a", "roles": ["hardware"], "clearance": 1},
    "hw-ops": {"scope": "ops-a", "roles": ["hardware"], "clearance": 1},
}
QUESTIONS = []


def q(
    number,
    query,
    relevant=(),
    *,
    principal="hw-a",
    scope="aurora-a",
    tags=(),
    expected_denial=False,
    conflict_required=False,
    counterpart=None,
    reason="",
):
    references = []
    for id, version in relevant:
        record = next(d for d in DOCUMENTS if (d["scope"], d["id"], d["version"]) == (scope, id, version))
        references.append(
            {
                "scope": scope,
                "id": id,
                "version": version,
                "source_hash": record["source_hash"],
                "status": "EXPIRED" if record["valid_until"] else "CURRENT",
            }
        )
    QUESTIONS.append(
        {
            "id": f"Q{number:02d}",
            "split": "heldout",
            "query": query,
            "principal": principal,
            "scope": scope,
            "tags": list(tags),
            "relevant": references,
            "expected_denial": expected_denial,
            "conflict_required": conflict_required,
            "counterpart": counterpart,
            "reason": reason,
        }
    )


q(1, "minimum required boot cycles", [("evt-boot", "v1")], tags=["english", "lexical"])
q(2, "thermal peak", [("thermal-budget", "v1"), ("thermal-report-2026", "v2")], tags=["english", "lexical"])
q(3, "BOM revision A3", [("npi-bom", "v2")], tags=["english", "lexical", "version"])
q(4, "CI smoke", [("ci-smoke", "v1")], tags=["english", "lexical"])
q(5, "rollback bundle", [("sre-rollback", "v1")], tags=["english", "lexical"])
q(6, "fastboot flash", [("android-flashing", "v1")], tags=["english", "lexical"])
q(7, "evt-boot", [("evt-boot", "v1")], tags=["exact-id"])
q(8, "npi-bom", [("npi-bom", "v2")], tags=["exact-id", "version"])
q(9, "thermal-budget", [("thermal-budget", "v1")], tags=["exact-id"])
q(10, "approval-rules", [("approval-rules", "v1")], tags=["exact-id"])
q(
    11,
    "startup endurance",
    [("evt-boot", "v1")],
    tags=["english", "synonym"],
    reason="Startup endurance refers to repeated boot cycles; no matching surface words are guaranteed.",
)
q(
    12,
    "heat ceiling",
    [("thermal-budget", "v1")],
    tags=["english", "synonym"],
    reason="Heat ceiling refers to maximum thermal temperature.",
)
q(
    13,
    "parts catalogue",
    [("npi-bom", "v2")],
    tags=["english", "synonym"],
    reason="Parts catalogue refers to bill of materials.",
)
q(
    14,
    "backout instructions",
    [("sre-rollback", "v1")],
    tags=["english", "synonym"],
    reason="Backout instructions refer to rollback.",
)
q(15, "发布", [("zh-release", "v1")], tags=["chinese", "lexical"])
q(16, "老化", [("zh-aging", "v1")], tags=["chinese", "lexical"])
q(17, "项目排期", [("pm-schedule", "v1")], principal="pm-a", tags=["chinese", "lexical"])
q(18, "权限", [("zh-permissions", "v1")], tags=["chinese", "lexical"])
q(19, "怎么刷入固件", [("android-flashing", "v1")], tags=["chinese", "cross-language"])
q(20, "最高允许温度", [("thermal-budget", "v1")], tags=["chinese", "cross-language"])
q(21, "release checklist", [("zh-release", "v1")], tags=["english", "cross-language"])
q(22, "burn-in test", [("zh-aging", "v1")], tags=["english", "cross-language"])
q(23, "bill of materials", [("npi-bom", "v2")], tags=["version", "lexical"])
q(24, "Which current BOM replaces A2?", [("npi-bom", "v2")], tags=["version", "english"])
q(25, "THERMAL-REPORT-2026", [("thermal-report-2026", "v2")], tags=["version", "exact-id"])
q(
    26,
    "Labnote boot current measurements",
    [("labnote-boot-pass", "v1"), ("labnote-boot-fail", "v1")],
    tags=["conflict", "english"],
    conflict_required=True,
    reason="Both current records describe build B7 boot count and contradict; retrieve both, flag disagreement independently from citation validity.",
)
q(
    27,
    "Which labnote records contradict boot count?",
    [("labnote-boot-pass", "v1"), ("labnote-boot-fail", "v1")],
    tags=["conflict", "english"],
    conflict_required=True,
)
q(
    28,
    "rf-archive",
    [("rf-archive", "v1")],
    tags=["expiry", "exact-id"],
    reason="Retrieve archive as EXPIRED; it cannot supply current positive gate evidence.",
)
q(29, "obsolete calibration chamber", [("rf-archive", "v1")], tags=["expiry", "lexical"])
q(30, "quantum lidar actuator", tags=["unanswerable", "english"], reason="No document covers this subject.")
q(
    31,
    "EVT antenna accreditation",
    tags=["unanswerable", "english"],
    reason="Generic EVT overlap does not establish antenna accreditation evidence.",
)
q(32, "无线充电技术", tags=["unanswerable", "chinese"], reason="No wireless charging document exists.")
q(
    33,
    "pm-risk",
    tags=["acl-negative", "role", "exact-id"],
    counterpart="Q34",
    reason="PM-only record must be unavailable to hardware.",
)
q(
    34,
    "pm-risk",
    [("pm-risk", "v1")],
    principal="pm-a",
    tags=["acl-control", "role", "exact-id"],
    counterpart="Q33",
)
q(
    35,
    "restricted-cost",
    [("restricted-cost", "v1")],
    principal="hw-high",
    tags=["acl-control", "classification", "exact-id"],
    counterpart="Q36",
)
q(
    36,
    "restricted-cost",
    principal="hw-low",
    tags=["acl-negative", "classification", "exact-id"],
    counterpart="Q35",
)
q(
    37,
    "evt-boot",
    scope="aurora-b",
    tags=["acl-negative", "tenant", "exact-id"],
    expected_denial=True,
    counterpart="Q38",
)
q(
    38,
    "evt-boot",
    [("evt-boot", "v1")],
    principal="hw-b",
    scope="aurora-b",
    tags=["acl-control", "tenant", "exact-id"],
    counterpart="Q37",
)
q(
    39,
    "evt-boot",
    scope="orion-a",
    tags=["acl-negative", "project", "exact-id"],
    expected_denial=True,
    counterpart="Q40",
)
q(
    40,
    "evt-boot",
    [("evt-boot", "v1")],
    principal="hw-orion",
    scope="orion-a",
    tags=["acl-control", "project", "exact-id"],
    counterpart="Q39",
)
q(
    41,
    "evt-boot",
    scope="ops-a",
    tags=["acl-negative", "workspace", "exact-id"],
    expected_denial=True,
    counterpart="Q42",
)
q(
    42,
    "evt-boot",
    [("evt-boot", "v1")],
    principal="hw-ops",
    scope="ops-a",
    tags=["acl-control", "workspace", "exact-id"],
    counterpart="Q41",
)
q(43, "supplier risk mitigation", tags=["acl-negative", "role", "lexical"], counterpart="Q44")
q(
    44,
    "supplier risk mitigation",
    [("pm-risk", "v1")],
    principal="pm-a",
    tags=["acl-control", "role", "lexical"],
    counterpart="Q43",
)
q(
    45,
    "EVT entry",
    [("evt-entry", "v1")],
    tags=["acl-decoy", "classification", "lexical"],
    reason="High-clearance decoy is the strongest lexical match but cannot be read by this principal.",
)
q(46, "NEVER-APPROVED-P1", tags=["acl-negative", "role", "exact-token"], counterpart="Q47")
q(
    47,
    "NEVER-APPROVED-P1",
    [("private-canary", "v1")],
    principal="pm-a",
    tags=["acl-control", "role", "exact-token"],
    counterpart="Q46",
)

DATASET = {
    "id": "aurora-lexical-heldout-v1",
    "schema_version": 1,
    "authoring": "Manually specified synthetic corpus and exact relevance. Human per-question gold review remains pending.",
    "split_policy": "All questions heldout from S1 scorer implementation. No training/tuning questions or model labels. Do not rewrite gold after seeing results.",
    "relevance_policy": "Gold is the direct source supplying the requested requirement, procedure or fact; incidental topical mentions are not relevant. Current version is default unless expiry is explicitly tested. Contradiction tasks require both records.",
    "temporal_policy": "Current rows have valid_until null; expired rows use 2000-01-01 UTC. Superseded versions never become new current references.",
    "top_k": 5,
    "repetitions": 5,
    "warmup": "One unmeasured search for each question, after freeze.",
    "scopes": SCOPES,
    "principals": PRINCIPALS,
    "documents": DOCUMENTS,
    "questions": QUESTIONS,
}

if __name__ == "__main__":
    if DATASET_PATH.exists() or (ROOT / "freeze.json").exists():
        raise SystemExit(
            "Refuse replacing existing corpus/gold: create a new explicitly versioned benchmark instead."
        )
    DATASET_PATH.write_text(json.dumps(DATASET, ensure_ascii=False, indent=2) + "\n")
    print(
        f"Authored {len(DOCUMENTS)} immutable version rows; {len(QUESTIONS)} heldout questions. No retrieval run yet."
    )
