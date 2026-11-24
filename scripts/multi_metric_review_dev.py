"""Create a local Apple two-metric → scoped-recheck → approval → export evidence bundle.

The two identities below are scripted local actors, not independent user testing.
This script only uses the registered public Apple PDF and its local parse cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow import apple_sales, research_store
from forgeflow.contracts import canonical, digest
from forgeflow.report_desk import SOURCES, ReportDesk, mount_report_desk


def run(reports: Path, output: Path, cache: Path | None):
    report = next(item for item in json.loads(SOURCES.read_text())["reports"] if item["key"] == apple_sales.DOCUMENT)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"FORGEFLOW_RESEARCH_DIR": temp + "/private"}):
        root = Path(temp)
        catalogue = root / "sources.json"
        catalogue.write_text(json.dumps({"reports": [report]}))
        if cache:
            parsed = cache / (report["sha256"] + ".json")
            if not parsed.is_file():
                raise RuntimeError("指定缓存不含 Apple PDF 指纹对应的解析结果。")
            (root / "ingest-cache").mkdir()
            shutil.copyfile(parsed, root / "ingest-cache" / parsed.name)
        desk = ReportDesk(reports, root, sources=catalogue, ollama_url="http://127.0.0.1:1")
        desk.load()
        if desk.reports[0]["state"] != "ok":
            raise RuntimeError("需要登记指纹对应的 Apple FY2025 PDF。")
        app = FastAPI()
        mount_report_desk(app, desk)
        users = {name: {"Authorization": "Bearer " + research_store.issue(root / "private", name, role)}
                 for name, role in [("demo-author", "author"), ("demo-reviewer", "reviewer")]}
        with TestClient(app, client=("127.0.0.1", 40100)) as client:
            def post(path, body, who, expected=200):
                response = client.post(path, json=body, headers=users[who])
                assert response.status_code == expected, response.text
                return response.json()

            full = post("/reports/ask", {"question": "Apple FY2025 服务收入增速和增长贡献"}, "demo-author")
            first = post("/research/drafts", {"grant_id": full["research_grant"], "note": "完整经营稿：保留增速和增长贡献。"}, "demo-author")
            path = "/research/drafts/" + first["id"]
            post(path + "/decisions", {"expected": first["head"], "action": "submit", "reviewer": "demo-reviewer"}, "demo-author")
            post(path + "/decisions", {"expected": first["head"], "action": "return", "memo": "请复核增长贡献，其他已核事实与说明保留。"}, "demo-reviewer")
            focused = post("/reports/ask", {"question": "Apple FY2025 服务收入增长贡献"}, "demo-author")
            second = post(path + "/revisions", {"expected": first["head"], "grant_id": focused["research_grant"],
                "note": "按意见复核增长贡献；保留服务收入增速和完整稿说明。", "preserve_existing_metrics": True}, "demo-author")
            assert [row["metric"] for row in second["payload"]["evidence"]["calculation"]["operations"]] == ["服务收入增速", "服务对公司收入增量的贡献"]
            assert second["payload"]["evidence"]["metric_patch"]["refreshed"] == ["服务对公司收入增量的贡献"]
            post(path + "/decisions", {"expected": second["head"], "action": "submit", "reviewer": "demo-reviewer"}, "demo-author")
            stale = client.post(path + "/decisions", json={"expected": first["head"], "action": "approve"}, headers=users["demo-reviewer"])
            assert stale.status_code == 409
            approved = post(path + "/decisions", {"expected": second["head"], "action": "approve"}, "demo-reviewer")
            delivered = post(path + "/decisions", {"expected": second["head"], "action": "deliver"}, "demo-author")
            assert approved["head"] == delivered["head"] == second["head"]
            artifacts = []
            for version, fmt in ((1, "json"), (2, "json"), (2, "html")):
                response = client.get(f"{path}/versions/{version}/export/{fmt}", headers=users["demo-author"])
                assert response.status_code == 200
                name = f"apple-multi-metric-v{version}.{fmt}"
                (output / name).write_bytes(response.content)
                artifacts.append({"file": name, "sha256": hashlib.sha256(response.content).hexdigest(), "bytes": len(response.content)})
            final = json.loads((output / "apple-multi-metric-v2.json").read_text())
            assert digest(final["payload"]) == final["digest"] == second["head"]
            manifest = {"schema_version": 1, "case_id": "apple-multi-metric-scoped-recheck-dev", "split": "public-dev",
                        "driver": "real FastAPI ASGI + SQLite, scripted local roles, verified public PDF",
                        "roles": {"demo-author": "scripted local author", "demo-reviewer": "scripted local reviewer"},
                        "model": None, "model_calls": 0, "external_cost": 0, "draft_id": first["id"],
                        "versions": [{"version": item["version"], "revision_digest": item["head"],
                                      "run_id": item["payload"]["evidence"]["run_id"]} for item in (first, second)],
                        "approval": {"old_digest_rejected": True, "approved_digest": second["head"],
                                     "delivered_digest": delivered["head"], "local_receipt": final["local_receipt"]},
                        "metric_patch": final["payload"]["evidence"]["metric_patch"], "artifact_refs": artifacts}
            (output / "multi-metric-manifest.json").write_text(canonical(manifest) + "\n")
            return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.reports, args.output, args.cache), ensure_ascii=False, indent=2))
