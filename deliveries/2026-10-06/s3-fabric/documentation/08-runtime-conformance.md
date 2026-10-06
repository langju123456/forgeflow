# Runtime handoff

Canonical runtime modules are in `src/forgeflow/`: `sandbox.py`, `templates.py`,
`mcp_bridge.py` and `scanner.py`. Temporary implementation staging is removed after integration.

Actual installed official MCP SDK is **1.30.0**. Independent client/server stdio
traffic negotiated **2025-11-25** and recorded serialized JSON-RPC
`initialize`, `notifications/initialized`, `tools/list`, and `tools/call` at the
transport boundary. This is genuine MCP. The server currently registers only
`sandbox.execute`; ordinary Knowledge/Memory and local IssueTracker service calls
must not be described as MCP tools yet.

Current policy grants execution only to the hardware role; other role adapters are pending.
Each child server is fixed to one trusted principal, role set, full project scope,
run, immutable ExecutionSpec, same-source successful scan receipt and lease expiry.
The task-owned 0600 bootstrap file is DEV_ONLY internal authority, not SSO or a
production capability service. Tool parameters cannot replace identity, image,
network, paths, flags or code. Hidden tools are also denied on direct calls.

Docker profile uses an offline content-addressed Python image ID, UID/GID 10001,
read-only root, network none, 0.5 CPU, 256 MiB RAM and same swap maximum, 32 PIDs,
ALL capabilities dropped, no-new-privileges, default seccomp, logging driver none,
64 MiB workspace tmpfs and 8 MiB temporary tmpfs. Bounded attach stores at most
256 KiB for each output stream, then stops the exact container. A runner timer
also stops an unpaused orphan; paused orphans require durable recovery.

The initial pause + `docker cp` experiment **failed**, because Docker explicitly
does not copy tmpfs files through this command. See [official Docker cp corner
cases](https://docs.docker.com/reference/cli/docker/container/cp/). The reviewed S1
amendment keeps the bounded tmpfs workspace and adds one exact task-owned
`result.json` writable bind, plus a hard inherited RLIMIT_FSIZE of 2 MiB.
After completion, the controller pauses the container and reads this exact file
with O_NOFOLLOW, regular-file, link-count, original inode, size, JSON and hash
checks. No directory archive is extracted. The result file is removed together
with its deterministic private scratch directory after exact container removal.

This file profile supports **trusted predefined templates only**. RLIMIT_FSIZE
is not a complete disk quota against arbitrary code or preallocation syscalls.
Agent-generated code remains blocked until a suitable quota/provider and its
adversarial tests are implemented. The same-UID runner is also not a second
security boundary. Config inspection proves configured limits; not every resource
exhaustion attack has been exercised.

Handle identity is `ff-` + first 32 hex characters of canonical SHA256 over
`{scope, run, step, attempt}`. The store persists it before starting MCP. A private
deterministic root has retained per-handle lifecycle locks and revocation markers.
Cleanup revokes an old attempt, stops/removes only its ownership-labelled
container, and removes only its allowlisted scratch files. A late old attempt
cannot start again after recovery. Unrecognizable partial scratch is reported
as cleanup failure for review; it is not silently claimed clean.

Unit/protocol command:

```sh
.venv/bin/python -m pytest tests/test_sandbox.py tests/test_mcp_bridge.py -q
```

Live conformance additionally requires authorized Docker access and the actual
pinned scanner image reported by the scanner build, supplied as
`FORGEFLOW_SCANNER_IMAGE`. `scripts/runtime_live_validate.py` writes evidence for real Semgrep,
MCP, Docker, timeout, output limits, network/filesystem probes, cancellation and
worker process interruption. It does not claim an LLM, production use, real
hardware certification or an external Issue.

Small learning exercise after the canonical demo works: add one typed synthetic
field to the fixture, update the trusted checker and independent validator, and
observe that the code hash changes and the old scan receipt is rejected. Record
the change and explain why a successful checker is distinct from gate readiness
and human approval. This exercise does not imply the user has already mastered
the backend or ML.
