#!/usr/bin/env python3
"""Read-only drift report: is anything out of step with the code in this repository?

    .venv/bin/python scripts/drift_check.py [--public ../forgeflow-public] [--skip-service]

Checks (exit code 1 if any is DRIFT, 0 otherwise; the report is JSON on stdout):
  git_clean       tracked files match HEAD (untracked files do not count)
  baseline_tag    the freeze-baseline tag points at the commit the frozen-evidence anchor names
  service         the process on loopback 8773 reports the fingerprint of the code now on disk
  public_mirror   (with --public) the public copy was built from this HEAD and has not been edited since

It only reads: git queries, one GET of http://127.0.0.1:8773/health, and the history map next to the public
copy. It never touches .runtime, a database, a model or any other port.
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = "deliveries/2026-10-06/s2c-fixes/evidence/freeze-git-anchor.json"
HEALTH_URL = "http://127.0.0.1:8773/health"


def git(repo, *args, keep_leading_space=False):
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout
    return out.rstrip() if keep_leading_space else out.strip()


def result(check, status, detail=None):
    return {"check": check, "status": status, "detail": detail or {}}


def check_git_clean(repo):
    status = git(repo, "status", "--porcelain", "--untracked-files=no", keep_leading_space=True)
    changed = sorted(line[3:] for line in status.splitlines())
    return result("git_clean", "DRIFT" if changed else "OK", {"changed": changed})


def check_baseline_tag(repo, anchor_file):
    anchor = json.loads(git(repo, "show", f"HEAD:{anchor_file}"))["git_commit"]
    tag = git(repo, "rev-parse", "refs/tags/freeze-baseline^{commit}")
    return result("baseline_tag", "OK" if tag == anchor else "DRIFT", {"anchor": anchor, "tag": tag})


def fetch_health(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only, never via a proxy
    try:
        with opener.open(url, timeout=3) as response:
            return json.loads(response.read())
    except urllib.error.URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            raise error.reason from None  # urllib wraps a refused connection; nothing is listening
        raise


def check_service(fetch, current):
    try:
        health = fetch(HEALTH_URL)
    except ConnectionRefusedError:
        return result("service", "INFO", {"reason": "NOTHING_LISTENING_ON_8773"})
    except (OSError, ValueError) as error:  # something listens (HTTP error, timeout, not JSON): cannot confirm
        return result("service", "DRIFT", {"reason": "SERVICE_DID_NOT_ANSWER_PROPERLY", "error": type(error).__name__})
    running = health.get("code_fingerprint")
    if running is None:
        return result("service", "DRIFT", {"reason": "HEALTH_HAS_NO_CODE_FINGERPRINT",
                                           "process_id": health.get("process_id")})
    detail = {"running": running, "current": current, "process_id": health.get("process_id")}
    return result("service", "OK" if running == current else "DRIFT", detail)


def check_public_mirror(repo, public):
    public = Path(public)
    map_file = Path(str(public) + ".history-map.txt")
    if not map_file.is_file():
        return result("public_mirror", "DRIFT", {"reason": "NO_HISTORY_MAP", "expected": str(map_file)})
    old, new = map_file.read_text().splitlines()[-1].split()
    head, public_head = git(repo, "rev-parse", "HEAD"), git(public, "rev-parse", "HEAD")
    detail = {"built_from": old, "head": head, "public_head": public_head, "published_tip": new}
    if public_head != new:
        return result("public_mirror", "DRIFT", {**detail, "reason": "PUBLIC_COPY_CHANGED_AFTER_PUBLISHING"})
    if head != old:
        return result("public_mirror", "DRIFT", {**detail, "reason": "PUBLIC_COPY_BEHIND"})
    return result("public_mirror", "OK", detail)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--anchor", default=ANCHOR)
    parser.add_argument("--public", help="path of the public copy built by publish_public_history.sh")
    parser.add_argument("--skip-service", action="store_true", help="do not look at port 8773")
    args = parser.parse_args(argv)
    checks = [check_git_clean(args.repo), check_baseline_tag(args.repo, args.anchor)]
    if not args.skip_service:
        sys.path.insert(0, str(Path(args.repo) / "src"))
        from forgeflow.code_fingerprint import code_fingerprint

        checks.append(check_service(fetch_health, code_fingerprint(args.repo)))
    if args.public:
        checks.append(check_public_mirror(args.repo, args.public))
    status = "DRIFT" if any(item["status"] == "DRIFT" for item in checks) else "OK"
    print(json.dumps({"status": status, "checks": checks}, indent=2))
    return 1 if status == "DRIFT" else 0


if __name__ == "__main__":
    sys.exit(main())
