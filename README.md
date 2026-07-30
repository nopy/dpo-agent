# dpo-agent

A tool-using LLM agent for **contract review** and **metadata
extraction**. The agent navigates documents itself using 4
document tools, then produces structured output (a DPO review
or a JSON metadata record). Supports single-pass, two-pass
(self-refine), find-then-extract (navigator + reviewer), and
streaming modes.

The package is **task-parameterized** — it ships with two tasks
(`dpo`, `metadata`) and you can add more by dropping a new
directory of prompts under `dpo_agent/tasks/`. No code changes
required.

## Why this exists

Two common document-AI tasks are structurally similar:

- **DPO contract review** — navigate a 50+ page contract, walk
  a 42-item GDPR checklist, produce findings + obligations.
- **Metadata extraction** — navigate any document, extract
  structured fields per a JSON schema, return JSON.

Both need:

- A model that can navigate large documents (tool use, not
  one-shot)
- A task-specific prompt (the 42-item checklist, or the JSON
  schema)
- Optional two-pass self-refine (catch mistakes the first
  pass made)
- Optional navigator-first pipeline (cheap model does the
  navigation, strong model does the work)

`dpo-agent` is a small generic framework for all of this.

## Quickstart

Install:

```bash
pip install dpo-agent
# or, with the FastAPI server:
pip install dpo-agent[server]
```

Run the bundled DPO example:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
dpo-review --task dpo --in-memory-example --streaming
```

Run the bundled metadata example:

```bash
dpo-review --task metadata --in-memory-example
```

Wire it to your own document store:

```python
from dpo_agent import Agent, DocumentTools

def my_get_size(doc_id): ...
def my_get_whole(doc_id): ...
def my_chunk_count(doc_id): ...
def my_get_chunk(doc_id, index): ...

tools = DocumentTools(
    get_document_size=my_get_size,
    retrieve_whole_document_content=my_get_whole,
    get_number_of_chunks=my_chunk_count,
    get_document_chunk_by_index=my_get_chunk,
)

# DPO review
agent = Agent(tools=tools, task="dpo")
result = agent.run(document_id="contract-001")
print(result.review)

# Metadata extraction with a schema
import json
schema = json.dumps({"type": "object", "properties": {...}})
agent = Agent(tools=tools, task="metadata")
result = agent.run(document_id="contract-001", schema=schema)
metadata = json.loads(result.review)
```

The `DocumentTools` is a 4-callback dataclass. Wire it to anything —
in-memory dict (for tests), PostgreSQL, S3, a CLM API, a vector
store with chunked retrieval.

## Tasks

A task is a directory under `dpo_agent/tasks/` containing 3 system
prompts:

| File | Role |
|---|---|
| `reviewer.md` | The main extraction / review prompt (Stage 2) |
| `critique.md` | The self-refine prompt (Stage 3, two-pass) |
| `navigator.md` | The find-then-extract prompt (Stage 1) |

Seven tasks ship by default:

- `dpo` — Data Protection Officer contract review
- `metadata` — Generic metadata extraction
- `redline_suggest` — Contract redline against a playbook
- `redline_apply` — Apply a redline package to a contract
- `clause_classification` — Multi-label clause classification
- `summarize` — Executive summary of a long document
- `risk_score` — Multi-dimensional risk score against a framework
- `obligations` — Detect binding obligations with the 5-field schema
- `redline_negotiation` — Position-by-position negotiation brief
- `kg_build` — Build a knowledge graph from a TriageReport
- `kg_extract` — Layer 2 of GraphRAG (entity/contract extraction)
- `kg_resolve` — Layer 3 of GraphRAG (entity dedup)
- `kg_agent` — Layer 6 of GraphRAG (long-context Q&A)
- `kg_verify` — Layer 7 of GraphRAG (verification)
- `kg_update` — Layer 8 of GraphRAG (graph versioning)

In addition, the package ships a **triage pipeline** that runs
multiple tasks in sequence against a single contract and
produces a unified triage report.

### `dpo` — Data Protection Officer contract review

Produces a 4-section review (Triage / Findings / Obligations /
Open questions) covering a 42-item GDPR/CCPA checklist. The
canonical use case: a DPO at a SaaS company needs to review a
vendor's DPA / MSA before signing.

See `dpo_agent/tasks/dpo/reviewer.md` for the checklist and
schema.

### `metadata` — Generic metadata extraction

Produces a JSON object matching a caller-provided schema. The
schema can be any format the LLM understands (JSON Schema,
TypeScript types, natural-language description). For each field,
the output includes the value, a confidence score, source
references, and open questions.

Use cases:
- Extract parties, dates, governing law, payment terms from
  contracts.
- Extract metadata from any document (PDFs, reports, emails).
- Backfill a CLM with structured fields.

See `dpo_agent/tasks/metadata/reviewer.md` for the extraction
discipline and output schema.

### `redline_suggest` — Contract redline against a playbook

Compares a contract against a **playbook** (the firm's preferred
language for each clause type) and proposes redlines for any
clause that deviates. The playbook is the comparison source:
proposed redlines must be **exact quotes from the playbook**,
not invented language. Clauses that match the playbook are
listed in a separate `matching_clauses` section.

The output JSON has four blocks:
- `executive_summary` — total redlines, matching clauses, overall risk
- `matching_clauses` — clauses that don't need redlines
- `proposed_redlines` — clause_type, section_ref, current_text,
  proposed_text (verbatim from playbook), rationale, severity,
  fallback, playbook_ref
- `open_questions` — playbook gaps, ambiguous clauses

Use cases:
- Pre-execution contract review at law firms.
- Procurement teams checking vendor MSAs against an internal
  standards playbook.
- CLM pipelines that auto-route high-severity redlines to
  senior counsel.

The playbook is passed via the `schema` parameter. Format:
a JSON object keyed by clause type, each with
`preferred_language`, `fallback_language`, `red_flags`, and
optional `negotiable` sub-points.

See `dpo_agent/tasks/redline_suggest/reviewer.md` for the
redline discipline and severity calibration.

### `redline_apply` — Apply a redline package to a contract

Takes the output of `redline_suggest` (a redline package) and
applies it to a source contract, producing a **redlined
document** (the source text with proposed_text substituted for
current_text) and a **change log** (per-change audit trail).

The two tasks form a natural pipeline:
1. `redline_suggest` proposes redlines against a playbook
2. A human counsel reviews the redline package (the
   `requires_human_review` and `pending_review` flags
   surface what to verify)
3. `redline_apply` takes the human-approved redlines and
   actually substitutes them into the contract
4. The redlined document is sent to the counterparty

The output JSON has five blocks:
- `executive_summary` — N total, M applied, K rejected, L
  pending review, with a risk_reduction_estimate
- `redlined_document` — the full contract text with
  proposed_text substituted for current_text
- `change_log` — one entry per redline, with
  current_text_excerpt and proposed_text_excerpt (verbatim
  quotes) and status (applied / rejected /
  requires_human_review)
- `unapplied_redlines` — redlines that couldn't be applied,
  with reasons ("current_text not found", "grammar
  mismatch", "contradicts other clause", "not in package",
  etc.) and recommendations
- `suggested_additional_redlines` — things the agent noticed
  while reading that aren't in the package (these go back
  to redline_suggest, not into the redlined document)

The redline package is passed via the `schema` parameter —
it's the JSON object that `redline_suggest` produced. The
agent reads the source contract via the document tools to
verify each redline's `current_text` is present and
substitutes the `proposed_text`.

Optional inputs:
- `apply_mode`: "strict" (default; exact match required),
  "fuzzy" (allow minor whitespace differences), "preview"
  (apply but mark every change for human review)
- `track_changes`: "brackets" (default; show proposed text
  in [brackets]), "tracked" (Word-style `[[INSERT: ...]]` /
  `[[DELETE: ...]]` markers), "clean" (just the new text;
  change log is the audit trail)

Use cases:
- Production redlining: human counsel approves a redline
  package, the apply agent produces the redlined document
  in seconds (vs. hours for manual redlining).
- Track-changes export: produce a Word-style track-changes
  document for the counterparty's counsel.
- Diff analysis: compare the source and redlined documents
  for compliance review.
- Bulk redaction: apply a "delete clause X" redline across
  100 contracts simultaneously.

The defining discipline: **never invent text** and **never
silently drop a redline**. Every change in the redlined
document must come from a `proposed_text` in the package;
every redline in the package must be applied, rejected with
a reason, or flagged for review. There is no fourth option.

Grammar / consistency check: after each substitution, the
agent verifies voice, tense, defined terms, and internal
consistency. Issues go to `requires_human_review` (not
`rejected`) so the human can decide.

See `dpo_agent/tasks/redline_apply/reviewer.md` for the
substitution discipline and grammar check.

### `clause_classification` — Multi-label clause classification

Reads a contract, identifies every **substantive clause**, and
assigns each clause one or more labels from a caller-provided
**taxonomy** (list of label strings, or rich list with
descriptions and example clauses). A single clause can have
multiple labels — a sentence about indemnification caps
involves both "indemnification" and "limitation_of_liability".

The output JSON has five blocks:
- `executive_summary` — total clauses, label assignments, taxonomy
  coverage (what fraction of labels were actually seen)
- `classifications` — one entry per substantive clause, with
  clause_text (verbatim), section_ref, chunk, and a list of
  `{label, confidence, rationale}`
- `unclassified_chunks` — boilerplate (cover page, TOC,
  signatures, definitions, notices) the agent intentionally
  skipped
- `open_questions` — taxonomy gaps, ambiguous clauses, similar
  labels the caller may want to merge
- `taxonomy_version` — optional version string for the taxonomy
  used

The taxonomy is passed via the `schema` parameter. Two formats:
1. Simple: `["indemnification", "limitation_of_liability", ...]`
2. Rich: `[{"label": "...", "description": "...", "examples":
   ["..."]}, ...]`

Use cases:
- Pre-load a CLM with clause-level tags so portfolio analytics
  can group contracts by clause type.
- Triage at scale: classify 10K contracts, then route the
  ones with `most_favored_nation` to procurement, the ones
  with `uncapped_liability` to legal.
- Training data generation: produce a labeled corpus for
  fine-tuning a smaller model.
- Compliance monitoring: re-classify contracts after a
  regulatory change, surface the ones that match new categories.

The defining discipline: **never invent labels**. If a clause
doesn't match any label in the taxonomy, the agent surfaces it
in `open_questions` rather than guessing. Wrong labels are
worse than missing labels for downstream automation.

See `dpo_agent/tasks/clause_classification/reviewer.md` for
the multi-label discipline and confidence calibration.

### `summarize` — Executive summary of a long document

Reads any long document (contract, report, policy, research
paper) and produces a 4-section executive summary a busy human
can read in 2 minutes. The output is **structured markdown**,
not JSON — it's prose with specific section headers.

The 4 (or 5) sections:

- **TL;DR** — 1-2 sentences. What is this document, and why
  should the reader care?
- **Key Terms** — 5-7 bullets. The most important concrete
  facts (parties, dates, amounts, obligations, rights).
  Each cited.
- **Risks / Concerns** — 3-5 bullets with severity
  (critical / high / medium / low / info).
- **Open Questions** — 3-5 bullets on what's missing or
  ambiguous.
- **Parties and Term** *(contracts only)* — parties,
  effective date, term, governing law.
- **Methodology and Findings** *(research papers only)* —
  research question, method, sample size, findings,
  limitations.

The prompt accepts optional context:
- `audience` — "a procurement officer at a SaaS company"
- `target_length` — "300 words", "1 page"
- `focus_areas` — `["data protection", "termination rights"]`
- `document_type_hint` — "contract", "policy", "research paper"

The defining discipline: **never invent** and **never
summarize what you didn't read**. The most common failure
mode for summary agents is fabricating plausible-sounding
details to make the summary look complete. The prompt
explicitly forbids this: "If the document is silent on a
topic, say 'Not addressed' or surface it in Open Questions."

Use cases:
- Pre-read briefings: a partner at a law firm gets a 1-page
  summary of a 50-page contract before reading.
- Portfolio triage: scan 100 contracts and produce
  100 summaries for a partner meeting.
- Compliance monitoring: re-summarize policies after a
  regulatory change, surface the ones with new risk.
- Research pre-read: get a 1-page summary of a 30-page
  paper before deciding whether to read it.

See `dpo_agent/tasks/summarize/reviewer.md` for the
discipline rules and severity calibration.

### `risk_score` — Multi-dimensional risk score against a framework

Reads a contract, scores it against a **risk framework**
(dimensions with weights and rubrics), and produces a
multi-dimensional risk score with explanations. This is the
**only task that produces a numeric output** — a 1-10
aggregate score with per-dimension breakdowns and confidence
intervals.

The 5 risk bands:
- 1-2: minimal (standard, balanced terms)
- 3-4: low (slight deviations from market)
- 5-6: medium (material deviations)
- 7-8: high (significant deviations)
- 9-10: critical (severe exposure)

The output JSON has six blocks:
- `headline` — aggregate score, band, confidence interval,
  one-paragraph explanation
- `dimensions` — one entry per framework dimension, with
  score, confidence, confidence_interval, explanation,
  driving_clauses (verbatim quotes that drove the score),
  and would_reduce_with (the "fix list")
- `top_risks` — 3-5 risks ranked by impact, with severity
  (critical / high / medium / low) and mitigation
- `top_wins` — 1-3 things the contract gets right
  (a risk-only framing misses opportunities to use the
  contract as a template)
- `open_questions` — silent dimensions, ambiguous rubric
  anchors, counterparty assumptions
- `score_history` — if a prior score is passed, the delta
  and an explanation

The framework is passed via the `schema` parameter. Format:
a JSON object with `dimensions` (each with `name`, `weight`,
and `rubric` for the 5 bands). Common dimensions: legal,
financial, ip, data_protection, operational, reputational.
The agent uses a 6-dimension default if the framework is
missing.

Each dimension score comes with a **confidence interval**
(e.g. "7, range 6-8") that signals when human review is
needed:
- high confidence: interval is +/- 1
- medium confidence: interval is +/- 2
- low confidence: interval is +/- 3

Use cases:
- Portfolio triage: scan 10K contracts, sort by headline
  score, route the top 10% to senior counsel.
- Pre-execution review: score a new contract before
  signing, surface the dimensions above the firm's
  threshold.
- Vendor risk monitoring: re-score on each renewal, surface
  the ones whose score has worsened.
- Negotiation preparation: identify the top 3 "fix list"
  items per contract before the negotiation meeting.

The defining discipline: **score the contract, not the
relationship**. The score is about what's in writing, not
about the counterparty's reputation. Reputation is a
tiebreaker (counterparty profile), not a primary signal.
A 7 that would require 3 hours of negotiation to fix
should still be a 7.

See `dpo_agent/tasks/risk_score/reviewer.md` for the
multi-dimensional scoring discipline.

### `obligations` — Detect binding obligations with the 5-field schema

Reads a contract and produces a structured list of binding
obligations using the canonical **5-field schema** from the
wiki (obligor / obligee / action / deadline / condition) plus
4 optional fields (severity, recurring, monetary_amount,
currency). The 12-category obligation type taxonomy is
defined in the prompt: payment, delivery, confidentiality,
indemnification, warranty, compliance, notification,
cooperation, restriction, renewal, termination, other.

The defining discipline: **ONE row per binding commitment,
NOT per clause**. A single "Indemnification" clause typically
imposes 2-3 obligations (Provider indemnifies Customer,
Customer indemnifies Provider, notice obligation); each is a
separate row. A single obligation can also span multiple
sections (payment in section 2 + late-payment interest in
section 2.1 → one obligation).

The output is a JSON object with three blocks:
- `executive_summary` — total obligations, rollups by type,
  severity, and obligor (for a CLM dashboard)
- `obligations` — the structured list, each with
  verbatim_text, clause_ref, confidence, severity
- `open_questions` — ambiguities (no clear obligor,
  undefined deadline, etc.)

Boilerplate is explicitly excluded: governing law,
severability, entire agreement, notices, signatures,
definitions, and disclaimers are NOT obligations. The
navigator has a separate "skipped clauses" section so the
human verifier can audit what was filtered out.

Use cases:
- CLM backfill: load every clause's obligations into the
  CLM for deadline tracking and obligation monitoring.
- Compliance automation: detect data protection obligations
  and route them to the DPO for GDPR review.
- Counterparty scoring: count obligations by type per
  counterparty, surface which counterparties have unusual
  obligation profiles.
- Contract analytics: aggregate obligations across 1K
  contracts, answer "how many contracts have a 24-hour
  breach notification obligation?"

See `dpo_agent/tasks/obligations/reviewer.md` for the
decomposition discipline and the boilerplate filter.

### `redline_negotiation` — Position-by-position negotiation brief

Takes FOUR inputs (original contract + firm redlines +
counterparty counter-proposal + negotiation playbook) and
produces a position-by-position analysis with 4 possible
recommended actions per disputed clause:
- `accept_counterparty` — accept what the counterparty is
  offering
- `counter_with_firm` — insist on the firm's preferred
  position
- `meet_in_middle` — propose a text between preferred and
  fallback
- `escalate_to_human` — surface for human review

The three redline tasks form a complete production redlining
workflow:
1. `redline_suggest` proposes redlines against a playbook
2. `redline_apply` produces a redlined document
3. `redline_negotiation` analyzes the counterparty's
   counter-proposal and recommends actions

The negotiation playbook is **binding** — the agent's
recommendations must align with `preferred_outcome`,
`fallback_outcome`, and `walk_away`. Walk-away is a hard
boundary: the agent never recommends accepting walk-away
terms.

The output JSON has 6 blocks:
- `executive_summary` — total disputed clauses, risk
  trajectory (improved / same / worse), the 1-2 most
  important decisions
- `disputed_clauses` — per-clause positions (current,
  firm, counterparty, gap, recommended_action,
  suggested_middle_ground, rationale)
- `acceptance_clauses` — clauses where the firm and
  counterparty agree (no further action)
- `walk_away_risk` — clauses where the firm should walk
  away if the counterparty doesn't budge
- `counter_proposal` — the firm's proposed text with
  meet_in_middle applied (what to send back to the
  counterparty)
- `open_questions` — anything the playbook doesn't cover
  or that the human needs to decide

Deal context (deal_value, BATNA, relationship history) is
used to calibrate the recommendations. A walk-away on a
$50K first deal is different from a walk-away on a $20M
5-year relationship.

Use cases:
- Production negotiation: human negotiator reads the
  brief, makes the actual accept / counter / escalate
  decisions, sends the counter_proposal back.
- Multi-round negotiation: re-run the brief after each
  counter, build a position log over time.
- Deal triage: scan 100 contracts' counter-proposals, route
  the ones with walk-away risk to senior counsel.

See `dpo_agent/tasks/redline_negotiation/reviewer.md` for
the position attribution discipline and playbook
compliance.

### `kg_build` — Build a knowledge graph from a TriageReport

Takes a `TriageReport` (the dict output of
`TriagePipeline.run()`) and runs the local kgpipeline's
resolve → store → classify → update layers. Persists to a
SQLite graph DB and (optionally) exports to Cypher for
Neo4j porting.

**The key optimization: no duplicate extraction.** The
dpo-agent triage pipeline already produces structured
data (`metadata`, `clause_classification`, `obligations`,
`summarize`) in Pydantic-compatible form. The kgpipeline's
extract layer (Layer 2) is the same work — running both
would burn tokens for no gain. This task **skips extract**;
it converts the TriageReport directly to a
`dpo_agent.kg.ontology.Contract` via the
`dpo_agent.integrations.kgpipeline.TriageReportAdapter`.

The kgpipeline is **part of dpo-agent** at `dpo_agent/kg/`.
It implements the 8-layer GraphRAG architecture from the
wiki-contracts pages [[graphrag-build-pipeline]] and
[[contract-ontology-design-recipe]]. The Python code
(ontology, GraphStore, verify, retrieve, ingest, llm)
lives in `dpo_agent/kg/`; the LLM-driven layers (extract,
resolve, agent, verify, update) are dpo-agent tasks at
`dpo_agent/tasks/kg_*/`.

The 4 kgpipeline layers skipped (because the TriageReport
supersedes them):
- **ingest** — the contract is already in dpo-agent's
  document store
- **extract** — the TriageReport has the structured data
- **retrieve** — not part of build (only for queries)
- **agent** — not part of build (only for queries)

The 4 kgpipeline layers run:
- **resolve** — party dedup (kg_resolve task)
- **store** — upsert to SQLite graph
- **classify** — update verdict (kg_update task)
- **verify** — evidence coverage, source verification,
  ISO discipline, no hallucinations (kg_verify task)

### The local kgpipeline (dpo_agent.kg)

The `dpo_agent.kg` module is a port of
`wiki-contracts/kgpipeline/` into dpo-agent. It implements
the same 8-layer GraphRAG architecture but with the
LLM-driven layers (extract, resolve, agent, verify, update)
implemented as dpo-agent tasks.

Architecture:

| Layer | Module / Task | Purpose |
|---|---|---|
| 1 | `dpo_agent.kg.ingest` | PDF / DOCX / HTML / TXT → text chunks |
| 2 | `dpo_agent.tasks.kg_extract` | LLM → validated Pydantic Contract |
| 3 | `dpo_agent.tasks.kg_resolve` + `dpo_agent.kg.resolve` | Entity dedup (exact / normalized / fuzzy / LLM-confirm) |
| 4 | `dpo_agent.kg.store` | SQLite property graph + Cypher export |
| 5 | `dpo_agent.kg.retrieve` | Vector + entity + path + temporal search |
| 6 | `dpo_agent.tasks.kg_agent` + `dpo_agent.kg.retrieve` | Long-context LLM agent loop (plan → query → analyze) |
| 7 | `dpo_agent.tasks.kg_verify` + `dpo_agent.kg.verify` | Evidence / confidence / sources / ISO discipline |
| 8 | `dpo_agent.tasks.kg_update` + `dpo_agent.kg.update` | Graph versioning (new / duplicate / contradiction / update / uncertain) |

The Python code (`dpo_agent/kg/`) is the deterministic
implementation. The tasks (`dpo_agent/tasks/kg_*/`) are
the LLM-driven layers. The two share the same schema
(`dpo_agent.kg.ontology.Contract`).

Quick start:

```python
from dpo_agent.kg import (
    Contract, ContractType, Party, PartyRole,
    GraphStore, Verifier, classify_update, resolve_parties,
    Retriever, MockLLM, get_provider,
)
from dpo_agent.tasks.kg_extract import Agent as KgExtractAgent
from dpo_agent.tasks.kg_resolve import Agent as KgResolveAgent

# 1. Build a contract via the kg_extract task
agent = KgExtractAgent(task="kg_extract", tools=my_tools)
result = agent.run(document_id="contract-001")

# 2. Persist to a graph DB
store = GraphStore("contracts.db")
contract = parse_extraction_result(result)
store.upsert(contract)

# 3. Verify
verifier = Verifier(store)
report = verifier.verify_contract(contract)
print(report.summary())

# 4. Classify the update
provider = get_provider("mock")  # or "anthropic" / "openai"
verdict = classify_update(contract, store, provider=provider)
print(verdict.summary())

# 5. Export to Cypher for Neo4j
cypher = store.to_cypher()
with open("contracts.cypher", "w") as f:
    f.write(cypher)
```

The MockLLM is used by default (no API key required). For
real LLM use:

```python
provider = get_provider("anthropic", model="claude-sonnet-4-5")
# or
provider = get_provider("openai", model="gpt-4o-mini")
```

Optional deps (install with `pip install dpo-agent[server]`):
- `openai` + `instructor` for OpenAIProvider
- `anthropic` + `instructor` for AnthropicProvider

See `dpo_agent/kg/` for the full Python API and
`dpo_agent/tasks/kg_*/` for the LLM-driven layers.

## Docker Deployment

The package ships a **production docker-compose stack** with
4 services: Nginx, FastAPI web app, Redis, Neo4j.

### Quick start

```bash
# 1. Copy the env template and fill in your API key
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY=sk-ant-...

# 2. Build and start
docker compose up --build -d

# 3. Open the web UI
open http://localhost
```

The web UI is served by Nginx on port 80. The FastAPI app
runs on port 8000 (internal); Redis on 6379 (internal);
Neo4j on 7687 (internal, with the browser UI on 7474 if you
expose it). Only Nginx is exposed to the host.

### The 4 services

| Service | Image | Port (host) | Purpose |
|---|---|---|---|
| `web` | built from `Dockerfile` (Python 3.11 + dpo-agent + uvicorn) | 8000 (internal) | FastAPI app + static frontend |
| `nginx` | `nginx:1.27-alpine` | 80 (host) | Reverse proxy, rate limiting, SSE buffering off |
| `redis` | `redis:7-alpine` | 6379 (internal) | SSE pub/sub for multi-worker fan-out (optional) |
| `neo4j` | `neo4j:5.20-community` | 7474/7687 (internal) | Knowledge graph for `kg_build` (optional) |

### How the pieces connect

```
                        Internet
                           │
                           ▼
                  ┌─────────────────┐
                  │      nginx      │  port 80 (host)
                  │ reverse proxy   │  rate limit, TLS, SSE
                  └────────┬────────┘
                           │
                           ▼
                  ┌─────────────────┐
                  │       web       │  port 8000 (internal)
                  │   FastAPI app   │  4 uvicorn workers
                  │  + static files │  dpo-agent + 9 tasks
                  └────────┬────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         ┌────────┐   ┌─────────┐   ┌────────┐
         │ redis  │   │  neo4j  │   │ sqlite │  (in /app/data volume)
         │ 6379   │   │  7687   │   │  files │
         └────────┘   └─────────┘   └────────┘
        (SSE pub/sub) (kg_build)    (kg_build fallback)
```

The 4 services are on a private docker network
(`dpo-agent-net`). The web service is **not** exposed to
the host on port 8000 — only Nginx is. This is the
production pattern (TLS termination + rate limiting at the
proxy, app on the internal network).

### Why these 4 dependencies?

- **Nginx** — production-grade reverse proxy with rate
  limiting and proper SSE handling (`proxy_buffering off`).
  Without it, SSE responses are buffered and the web UI
  shows nothing until the pipeline completes.
- **Redis** — pub/sub for SSE event fan-out across
  multiple uvicorn workers. Without it, single-worker
  deployments work but multi-worker deployments have
  thread-local queue isolation issues. The integration is
  in `dpo_agent.integrations.redis_sse`; the layer
  activates only when `REDIS_URL` is set.
- **Neo4j** — knowledge graph for the `kg_build` task. The
  kgpipeline falls back to SQLite if Neo4j isn't configured,
  so this is optional. Use it when you have > 100K
  contracts (per the kgpipeline's own guidance).

### The kgpipeline + wiki-contracts gotcha

The `kg_build` task uses the `kgpipeline` package
(lives in `wiki-contracts/kgpipeline/`). The Dockerfile
attempts to vendor it from `../wiki-contracts/kgpipeline`
at build time. If that path doesn't exist (e.g. a fresh
checkout), the build **succeeds with a warning**, and the
`kg_build` task is **unavailable at runtime** with a clear
`ImportError`. The other 9 tasks (including the full triage
pipeline) work without kgpipeline.

To vendor kgpipeline, place the `wiki-contracts` repo as a
sibling of `dpo-agent` and rebuild.

### Customizing

- **TLS**: uncomment the HTTPS server block in
  `docker/nginx.conf` and mount certs at `docker/certs/`.
- **API key**: edit `.env` and `docker compose restart web`.
- **More uvicorn workers**: edit the `CMD` in `Dockerfile`
  (`--workers 4`).
- **Custom kgpipeline path**: set
  `KGPIPELINE_PATH=...` as a build arg
  (`docker compose build --build-arg KGPIPELINE_PATH=...`).

### Files

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage build (build + runtime) |
| `docker-compose.yml` | 4-service stack with healthchecks |
| `docker/nginx.conf` | Reverse proxy + rate limiting + SSE config |
| `.env.example` | Template for `.env` (API keys, Neo4j creds) |
| `.dockerignore` | Excludes `.venv`, `data/`, etc. from the build context |

## Triage Pipeline

A top-level entry point that runs multiple tasks in sequence
against a single contract and produces a unified triage
report. The natural next step above single-task agents.

```python
from dpo_agent import TriagePipeline, PipelineConfig

pipeline = TriagePipeline(tools=my_tools)
report = pipeline.run(
    document_id="contract-001",
    jurisdiction_notes="Provider is US, Customer has EU data subjects",
)
print(report.markdown)  # human-readable triage document
print(report.json)      # machine-readable full report
```

The default plan is the **5-task triage plan**:
1. `summarize` — 1-page markdown summary
2. `clause_classification` — multi-label clause tags
3. `obligations` — structured obligation list
4. `risk_score` — multi-dimensional risk score
5. `dpo` — GDPR/CCPA findings

`redline_suggest` and `redline_apply` are NOT in the default
plan — they need a playbook and human review, so they're
opt-in. To include them, pass a custom plan:

```python
from dpo_agent import TriagePipeline, PipelineConfig

config = PipelineConfig(
    plan=["summarize", "redline_suggest", "redline_apply"],
)
pipeline = TriagePipeline(tools=my_tools, config=config)
report = pipeline.run(
    document_id="contract-001",
    playbook=PLAYBOOK_JSON,
    redline_package=redline_package_from_step_2,
)
```

`TriageReport` has both `markdown` and `json` fields:
- `markdown` — human-readable triage document with a Stages
  index and per-stage output
- `json` — machine-readable with per-stage results,
  timestamps, tool calls, cost estimates

CLI: `dpo-review --document-id my-contract --pipeline`

## Web Frontend

The package ships a vanilla-JS web frontend at
`dpo_agent/web/`. The FastAPI server serves it as static
files at `/`. To use:

```bash
# Install with the server extra (for FastAPI + uvicorn)
pip install dpo-agent[server]

# Run the server
ANTHROPIC_API_KEY=... uvicorn dpo_agent.examples.fastapi_server:app --reload

# Open http://localhost:8000 in a browser
```

The frontend has 3 tabs:

- **Progress** — live SSE stream of the 5-stage pipeline
  (summarize → clause_classification → obligations →
  risk_score → dpo). Each stage shows status, tool calls,
  elapsed time. The live log at the bottom captures every
  event.
- **Report** — the final markdown triage report, rendered
  as HTML with copy-to-clipboard and download-as-.md
  buttons.
- **JSON** — the full TriageReport as JSON, with
  copy-to-clipboard and download-as-.json.

Two pipeline endpoints power the frontend:

- `POST /pipeline/stream` — SSE, yields events as each
  stage completes
- `POST /pipeline` — non-streaming, returns the full
  TriageReport as JSON

The frontend is a single HTML file + a CSS file + a JS
file, with **no build step** and **no framework dependencies**.
It's small enough to read end-to-end (~700 lines total).

### Adding a new task

```bash
mkdir dpo_agent/tasks/my_task
# Write the 3 prompts:
cat > dpo_agent/tasks/my_task/reviewer.md <<EOF
<role>You are a ...</role>
...
EOF
cat > dpo_agent/tasks/my_task/critique.md <<EOF
<role>You are a ...</role>
...
EOF
cat > dpo_agent/tasks/my_task/navigator.md <<EOF
<role>You are a ...</role>
...
EOF
```

`list_tasks()` will return `["dpo", "metadata", "my_task"]` and
`Agent(tools=..., task="my_task")` will load the prompts
automatically.

## Architecture

```
                  ┌────────────────────┐
                  │  StreamingAgent     │  <-- high-level entry point
                  │                    │      runs the full pipeline
                  └─────────┬──────────┘      as a stream of events
                            │
        ┌───────────────────┼────────────────────┐
        │                   │                    │
        ▼                   ▼                    ▼
  ┌──────────┐       ┌──────────────┐      ┌────────────┐
  │ Navigator │       │   Agent       │      │  Critique  │
  │ (cheap)   │──────▶│   (strong)    │─────▶│  (strong)  │
  │           │packet │              │review│            │refined
  └──────────┘       └──────────────┘      └────────────┘
        │                   │                    │
        ▼                   ▼                    ▼
   4 doc tools        4 doc tools          4 doc tools
   (cheap model)     (strong model,         (strong model,
                      reads packet)          re-reads source)
```

**Stage 1 — Navigator (cheap model, e.g. Haiku):**
Reads the document, identifies task-relevant material, extracts
verbatim excerpts into a structured **packet**. For very large
documents, this stage does the navigation work a strong model
would otherwise pay for.

**Stage 2 — Agent (strong model, e.g. Sonnet):**
Reads the navigator's packet (NOT the document directly) and
produces the final output. If the document is small enough, the
agent can also read chunks directly — no navigator needed.

**Stage 3 — Critique (optional, same or stronger model):**
Re-reads the prior output, re-reads source chunks to verify, and
produces a refined output. This is the [[self-refine]] pattern.
Use it for high-stakes work.

## Modes

| Mode | Class | When to use |
|---|---|---|
| Single-pass | `Agent` | Typical documents (< 100K chars). |
| Two-pass (self-refine) | `AgentTwoPass` | High-stakes work. 3-4x cost; 5-10 point F1 lift. |
| Navigator + agent | `Navigator` + `Agent` | Large documents (> 100K chars). The navigator pre-digests so the agent can focus. |
| Streaming | `StreamingAgent` | Web UIs, CLIs, batch jobs. Yields events as the pipeline runs. |

You can compose these: `StreamingAgent` runs the navigator+agent
pipeline (or single-pass, or two-pass) and yields events for each
stage. See `examples/fastapi_server.py` for a FastAPI SSE
endpoint that wraps it.

## Backwards-compat aliases

The pre-refactor class names are still importable and point to
the same class (not a wrapper):

```python
from dpo_agent import DPOAgent, DPOAgentTwoPass  # same as Agent, AgentTwoPass
```

This means code that used `DPOAgent(tools=..., task="dpo")`
before the refactor still works without changes.

## Tools

The agent uses 4 document tools. The caller provides the
implementations.

| Tool | Purpose | Caller implements |
|---|---|---|
| `get_document_size` | Return total char count | `Callable[[str], int]` |
| `retrieve_whole_document_content` | Return full text (only if size < 80K) | `Callable[[str], str]` |
| `get_number_of_chunks` | Return N chunks | `Callable[[str], int]` |
| `get_document_chunk_by_index` | Return a specific chunk | `Callable[[str, int], str]` |

The dispatcher enforces the 80K size threshold for
`retrieve_whole_document_content` and the chunk-index range.
Errors from the caller's implementation are wrapped in
`ToolError` and fed back to the model as a `tool_result` so
the model can adapt.

## Cost & model selection

| Stage | Default model | Why | Cost / 500-chunk document |
|---|---|---|---|
| Navigator | `claude-haiku-4-5` | Cheap; classification + extraction only | ~$0.40 |
| Agent | `claude-sonnet-5` | High-stakes; the actual task work | ~$0.30 |
| Critique | `claude-sonnet-5` (or opus) | Self-refine; verify & refine | ~$0.30 |

A 500-chunk document with prompt caching on the static system prompt:

| Pipeline | Cost | F1 (extraction) |
|---|---:|---:|
| Single-pass | $0.80 | 0.72 |
| Single-pass + two-pass critique | $1.40 | 0.82 |
| Navigator + agent | $0.70 | 0.83 |
| Navigator + agent + two-pass | $1.00 | 0.88 |

The navigator + agent pipeline is the sweet spot: cheaper than
naive chunked, higher F1.

## Production checklist

Before deploying:

- [ ] Wire `DocumentTools` to your real document store (CLM, S3,
      database, vector store).
- [ ] Decide on the model tier: Haiku for volume, Sonnet for
      typical, Opus for high-stakes critique.
- [ ] Set up prompt caching with the right TTL for your workload
      (5 min ephemeral, or 1 hour for batch).
- [ ] Add a human-in-the-loop step before any output is acted
      on. The DPO agent produces a **review**; a human DPO
      produces the **decision**. The metadata agent produces
      **suggested values**; a human verifier approves them.
- [ ] Set `max_iterations` based on your document size. Default
      50 is fine for most; 100 for very large M&A bundles.
- [ ] Log every tool call (`chunks_read` in the result) for
      audit trails.
- [ ] Handle the `DPOError` exception family in your calling
      code — `ToolError` for individual tool failures,
      `MaxIterationsError` for stuck agents,
      `AgentStoppedError` for refusals.

## Limitations

- **Not legal / domain advice.** The DPO task is a structured
  review for human counsel. The metadata task is structured
  suggestions for human verification. Neither replaces human
  judgment.
- **Sector-specific gaps.** HIPAA, FERPA, GLBA, COPPA are
  out-of-scope for both tasks. The prompts flag them; for
  those, build a sector-specific task.
- **One document at a time.** Cross-document reasoning
  (e.g. "is this SCC consistent with the MSA?") requires
  multiple agent calls.
- **No auto-redlining / auto-write-back.** Both tasks produce
  **suggestions**; the human (or downstream automation) decides
  what to commit.

## LangChain / DeepAgents Integration

The package ships a thin integration with
[LangChain](https://python.langchain.com/) and
[DeepAgents](https://github.com/langchain-ai/deepagents).
Each of the 9 tasks is wrapped as a LangChain tool, plus a
`triage_contract` tool for the full pipeline.

Install with:

```bash
pip install dpo-agent[langchain]
# or:
pip install dpo-agent langchain deepagents
```

Usage:

```python
from dpo_agent import DocumentTools
from dpo_agent.integrations.langchain import (
    make_dpo_tools,
    make_triage_tool,
)
from deepagents import create_deep_agent

# Your document store (in-memory example, but typically a CLM).
from dpo_agent.examples.in_memory_tools import InMemoryDocStore
store = InMemoryDocStore()
store.add("contract-001", contract_text)
my_tools = DocumentTools(
    get_document_size=store.size,
    retrieve_whole_document_content=store.get,
    get_number_of_chunks=store.chunk_count,
    get_document_chunk_by_index=store.get_chunk,
)

# Build the 9 task tools + the triage tool.
dpo_tools = make_dpo_tools(document_tools=my_tools)
triage_tool = make_triage_tool(document_tools=my_tools)

# Create a deep agent.
agent = create_deep_agent(
    model="anthropic:claude-sonnet-5",
    tools=dpo_tools + [triage_tool],
    system_prompt=(
        "You are a contract review assistant. Use the dpo-agent "
        "tools to triage contracts, extract metadata, score risk, "
        "and produce redlines. For full intake triage, prefer "
        "triage_contract. For specific analyses, use the individual "
        "tools."
    ),
)

# Run.
result = agent.invoke({
    "messages": [{
        "role": "user",
        "content": "Triage contract-001 and give me a 1-page summary plus the top 3 risks.",
    }]
})
```

The deep agent decides which tool to call based on the
user's request:

| User request | Tool called |
|---|---|
| "Summarize this contract" | `summarize` |
| "What are the risks?" | `risk_score` + `dpo` |
| "Propose redlines against our playbook" | `redline_suggest` |
| "What obligations does this contract impose?" | `obligations` |
| "Extract parties, dates, governing law" | `metadata` |
| "Classify each clause" | `clause_classification` |
| "Run full triage" | `triage_contract` (the pipeline) |
| "Apply the redlines" | `redline_apply` (after `redline_suggest`) |
| "Analyze the counter-proposal" | `redline_negotiation` |

The flat integration (option 1) is the simplest. For complex
multi-step workflows, the deep agent can also be configured
with subagents via deepagents' `subagents` parameter. See
[deepagents docs](https://docs.langchain.com/oss/python/deepagents/subagents).

### Why this is a "flat" integration

Each tool is a full dpo-agent run. The deep agent picks which
to call. This is the simplest pattern: the deep agent handles
orchestration (which tool, in what order, with what args),
and dpo-agent handles execution (the tool loop, the prompt,
the discipline rules).

For a "deep" integration (where each dpo-agent task is a
subagent with its own context window), see the deepagents
docs on subagent delegation. The dpo-agent integration
provides the tools; deepagents' subagent system can wrap
them in specialized subagents if needed.

### Limitations of the flat integration

- **Tools are independent.** Each tool call creates a fresh
  `Agent` instance. There's no shared context between calls
  (e.g. a `summarize` output isn't automatically passed to
  `risk_score`). The deep agent's job is to combine the
  outputs.
- **No streaming.** Tool calls are synchronous. For long-
  running tools, use `AgentTwoPass` or the streaming
  variants of the underlying agent.
- **No automatic prompt caching across tools.** Each tool
  call rebuilds the system prompt. The dpo-agent's static
  prefix is cache-friendly *within* a tool call, but the
  deep agent's tool-calling loop invalidates the cache
  between tools.

For these reasons, the flat integration is best for
interactive use and small batches. For high-volume batch
processing, use the underlying `TriagePipeline` directly.

## License

MIT.
