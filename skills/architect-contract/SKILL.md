---
name: architect-contract
description: Reconcile recorded hardware BOM evidence with the controlled synthetic Android interface and exposure contract.
allowed-tools: execution.receipts.read execution.events.read domain.artifacts.read
metadata:
  version: domain-source-review-v1
---

Read only the supervisor-authorized parent receipts and scoped, fixed domain artifacts.
Verify the parent result artifact hash, size, fenced provenance, input hash and event binding before using its BOM.
Compare measured, current, unconflicted hardware BOM with Android BOM metadata and the synthetic contract.
Compare package, SDK levels, protocol, launcher identity and explicit exposure values with the contract.
Missing or expired hardware evidence is UNKNOWN; observed contract contradictions are FAIL.
This deterministic SOP performs source reconciliation only. It cannot approve, execute business actions, change stages or claim hardware certification.
