---
name: sre-operations
description: Review recorded execution reliability, fenced completion and removal receipts separately from technical gates and human approval.
allowed-tools: execution.receipts.read execution.events.read
metadata:
  version: sre-recorded-operations-v1
---

Read only the supervisor-authorized execution and event projections.
Check completion binding, fenced lease closure, terminal cleanup, recorded resource and scan profile, and MCP receipts.
Inspector state was captured during artifact export: paused / Running=true does not mean the resource still exists.
Missing proof is UNKNOWN; an observed adverse receipt requires operational review.
Keep operational readiness, technical gate and human approval separate. Do not approve, dispatch, retry, repair or launch a sandbox.
This is a deterministic SOP over historical receipts, not an LLM agent or live infrastructure probe.
