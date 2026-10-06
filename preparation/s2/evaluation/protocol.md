# Frozen lexical retrieval baseline v1

This is a manually specified **synthetic** corpus and relevance
set; the user has not independently labelled it. It is held out from S1 scorer
implementation and tuning, not independently sampled hardware or enterprise data.
Before any heldout retrieval, validate and freeze corpus/gold, evaluator identity,
protocol and unchanged S1 source/manifest digests. Do not adjust labels or queries
after seeing results. A later dataset or evaluator change needs a new explicit
version retaining original inputs/results. If these tasks are used to tune, use
another untouched heldout set for the resulting quality claim.

Only existing S1 KnowledgeService and Store are used. The fresh SQLite database
and fixture principals live under this preparation root; no existing runtime
configuration, secret, Docker, network, model or embedding is used. Source content
is inserted in order; Store selects the last immutable version as current. Current
records use null valid_until, expired records use 2000-01-01 UTC, so no S1 clock
monkeypatch is needed. Superseded version references cannot be positive current gold.

The corpus has 26 immutable version rows in four full scopes. There are 47 frozen
queries: 37 with nonempty relevance, 3 unanswerable and 7 ACL negatives. Seven of
those 37 positives are explicit privileged ACL controls. Core-content retrieval
excludes these controls and reports 30 questions separately. The seven ACL negatives
and one positive with a forbidden strongest-match decoy form eight adversarial
exposure queries. All 47 queries and every measured repetition are checked for unauthorized returned candidate
and citation metadata, including scope, roles and classification. The transparent
RecordingStore records unchanged real Store outputs; it injects no metadata and
performs no post-filtering to make retrieval look safer.

Recall@5 is macro mean over the 37 nonempty-gold queries of
`unique retrieved exact refs intersect gold / gold count`. Exact ref identity is
(scope, logical ID, version, source_hash). Micro relevant-reference recovery is
also reported with its numerator and denominator. MRR@5 is macro mean of reciprocal
first relevant rank, or zero for a miss. Empty-gold queries have no Recall/MRR value;
they do not contribute vacuous perfect recall. Duplicate results consume actual
rank positions but cannot inflate recovered relevant-reference count.

Citation resolves and citation exact/authorized are separate. Each returned hit is
bound to the actual first-repetition repository citation and candidate origin,
including full scope, immutable DocumentVersion UUID and classification. Its actual
UUID is independently resolved and also compared with the frozen seeded identity
(ContextHit itself has no citation_id). Scope is taken from traced source metadata,
not reconstructed from the requested scope. Foreign same-ID/version/content/hash
twins and a forged lower hit classification cannot be blessed by resolving the
expected source instead.
A structurally valid EXPIRED citation is not a leak and is not current-usable
positive evidence. Current-usable fraction and status correctness are reported
separately, along with correct-status gold coverage so conditional success does not
hide misses. Here current-usable means exact, authorized, CURRENT provenance/status only; it
does not imply relevance, absence of contradictory sources, or Gate input eligibility.
These metrics count returned citations only; no returned citation gives
N/A, not 100%. They measure provenance/status, not answer relevance.

Two conflict tasks require retrieving both contradictory current documents and
also expect a CONFLICT flag. Real S1 Store has no cross-document subject_key field,
so failure to flag disagreement is a documented diagnostic, independent from
retrieval recall and exact citation correctness. No fake ContextStore enriches this
baseline, and literal different text is not claimed as semantic contradiction
reasoning. The fixture's contradiction was manually specified before execution.

ACL leak query rates use denominators 7 negatives, 8 adversarial exposures and all
47 queries; candidate/citation forbidden row counts are reported too. Privileged
controls must retrieve the same exact-query target (paired-control recovery is reported), to rule out lexical nonmatch
masquerading as ACL protection. Failed scope authorization must be uniform 404.
Security row counts cover all measured repetitions. Retrieval/citation quality retains
the first measured result per query, with all five results required to agree for
the stability diagnostic. The report also records unknown returned-source identities.
Unanswerable empty-context/nonempty-context rates are separate from ACL. Legal but
irrelevant context is a retrieval false positive, not an ACL leak. There is no LLM,
so no answer abstention or hallucination rate is claimed.

Latency uses perf_counter_ns around the real search call, including its repository
ACL and citation reauthorization. It excludes seeding and independent evaluator
citation validation. One unmeasured warmup search per query happens after freeze;
then five measured repetitions yield 235 calls. Report every query's samples and
aggregate median, nearest-rank p95 and maximum, with positive/ACL-negative/unanswerable
slices. These are warm local SQLite measurements, not pgvector, cold indexing,
network model latency, concurrency or production throughput.

The baseline has no prescribed target score. Keep all misses and false positives.
The final S1 manifest is verified before and after execution, and is never edited.
Future keyword/vector/RRF comparisons must use the same frozen corpus/questions,
version identities and ACL policy; changed policy or data gets a separate run/version.
