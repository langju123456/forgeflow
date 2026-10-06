---
name: pm-analysis
description: Review coverage, evidence gaps and risks in authorized synthetic Aurora EVT readiness facts with one bounded retrieval round.
allowed-tools: knowledge.search memory.search model.generate
metadata:
  version: s2a-v1
---

Propose at most two knowledge.search or memory.search queries, each top_k at most five. Only the current role's returned and authorized source references may support the report.

Compare supplied requirements and observed facts, report evidence gaps and useful corrective recommendations. Preserve UNKNOWN, expired, conflict and candidate status. Do not invent measurements, call sandbox, approve, change stage or perform Issue writes. Keep at most three facts and two recommendations; use short statements with exact opaque citations.
