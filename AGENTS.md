# AGENTS.md — guide for AI agents working in this codebase

This file is for LLM-based agents (you) to be productive quickly. It
captures the **architecture, conventions, and gotchas** that aren't
obvious from reading the code alone. The README.md is user-facing
(how to install, how to use the API). This file is LLM-facing.

## What this project is

**dpo-agent** is a task-parameterized LLM agent framework for
**contract review and metadata extraction**, with a built-in 8-layer
GraphRAG knowledge-graph pipeline. It's the
`dpo_agent.tasks.<task_name>` factory plus the surrounding infrastructure
to run those tasks against any LLM via a backend-agnostic `LLMClient`.

The project lives at `~/dpo-agent/`. The venv is at
`/home/npinot/.hermes/hermes-agent/venv/`.

## 1. Two faces of the codebase

The codebase has **two distinct subsystems** glued together by the
*triage pipeline*. Both use the same LLMClient backend, but their
shapes are different:

### 1a. Tool-using agents (most of the code)

The 10 "agentic" tasks — `dpo`, `metadata`, `redline_suggest`, etc.
— are tool-using loop agents. They load large contracts via 4
chunk tools, run a Claude-style tool_use loop, and emit a final
report.

```python
from dpo_agent import Agent, DocumentTools
from dpo_agent.examples.in_memory_tools import InMemoryDocStore

store = InMemoryDocStore(chunk_size=4000)
store.add("contract-001", contract_text)
tools = DocumentTools(
    get_document_size=store.size,
    retrieve_whole_document_content=store.get,
    get_number_of_chunks=store.chunk_count,
    get_document_chunk_by_index=store.get_chunk,
)
result = Agent(tools=tools, task="dpo").run(document_id="contract-001")
```

Lives in `dpo_agent/agent.py`, `navigator.py`, `two_pass.py`,
`streaming.py`. All four classes share the **same Anthropic-shaped
tool loop** — they just differ in what to do with the result (final
review, navigator packet, critique, streamed events).

### 1b. Structured-output knowledge graph (the `kg/` modules)

The 5 "kg" tasks — `kg_extract`, `kg_resolve`, `kg_agent`,
`kg_verify`, `kg_update` — are **LLM-driven layers** of an 8-layer
GraphRAG pipeline. The Python code (Pydantic schemas, GraphStore,
parsers, MockLLM) lives in `dpo_agent/kg/`. The LLM-driven layers
delegate to the same `Agent`/`Navigator` primitives as 1a (via
`dpo_agent.tasks.kg_*` prompts).

```python
from dpo_agent.kg import Contract, GraphStore

store = GraphStore(":memory:")
# ... populate from a TriageReport via TriageReportAdapter
```

The `dpo_agent.integrations.kgpipeline` module provides the
glue — it converts a `TriageReport` (output of the triage pipeline)
to a `Contract` (kg/ontology.py schema) and persists it.

## 2. The task directory convention

**Every built-in task lives at `dpo_agent/tasks/<task_name>/` and
has exactly 3 prompt files**:

- `reviewer.md` — the main extraction / analysis prompt
- `critique.md` — the self-refine / two-pass prompt
- `navigator.md` — the find-then-extract prompt

Tasks are auto-discovered by `dpo_agent.tasks.loader.list_tasks()`.
To add a new task: drop 3 .md files in a directory. The
`Agent(task=...)`, `Navigator(task=...)`, and `AgentTwoPass(task=...)`
classes pick up the prompts automatically. **There is no
registration step.**

The 3-prompt structure is non-negotiable. The tool-use loop assumes
each prompt exists; `load_prompt(task, "reviewer")` will raise
`TaskNotFoundError` if you forget one.

**The 15 current tasks are auto-discovered** — `list_tasks()` returns
`['clause_classification', 'dpo', 'kg_agent', 'kg_build', 'kg_extract',
'kg_resolve', 'kg_update', 'kg_verify', 'metadata', 'obligations',
'redline_apply', 'redline_negotiation', 'redline_suggest',
'risk_score', 'summarize']`.

## 3. The LLMClient abstraction (don't couple code to SDKs)

**Don't import `anthropic` or `openai` directly anywhere except in
`dpo_agent/llm_client.py`.** All other code calls
`self.client.create(...)` or `self.client.stream(...)` and
iterates `LLMResponse.content` for `TextBlock`/`ToolUseBlock`.

The abstraction has 3 backends:

- **`AnthropicClient`** — Anthropic direct
- **`OpenAICompatClient`** — OpenAI SDK pointed at any
  OpenAI-compatible endpoint (OpenRouter, OpenAI direct,
  Together, Groq, etc.). Wire-format translation lives here.
- **`MockClient`** — deterministic, used by tests

Auto-detection (`create_client()`) priority order:

1. `LLM_BACKEND` env var
2. `ANTHROPIC_API_KEY` → anthropic
3. `OPENROUTER_API_KEY` → openai-compat with
   `base_url=https://openrouter.ai/api/v1` and the
   `HTTP-Referer`/`X-Title` headers OpenRouter expects
4. `OPENAI_API_KEY` → openai-compat
5. Otherwise → mock

When testing new code paths, use `MockClient` from
`dpo_agent.llm_client`. Don't reach for `unittest.mock.patch` on
the SDK unless you need to test SDK-specific behavior.

## 4. Tests — conventions & patterns

Tests live in `tests/`. **22 test files, 408 passing.**

Key conventions:

- **No real API calls.** All tests are unit-only. The `MockClient`
  covers LLM-driven code paths; never use `@patch` on the
  Anthropic SDK unless you specifically need to verify SDK-shape
  interactions.
- **Construct `DocumentTools` from `InMemoryDocStore`** in your
  test fixture. Don't construct it with positional dict — it
  has 4 required tool functions as fields:

  ```python
  from dpo_agent.examples.in_memory_tools import InMemoryDocStore
  from dpo_agent import DocumentTools

  store = InMemoryDocStore(chunk_size=4000)
  store.add("example-dpa", "Tiny contract text...")
  tools = DocumentTools(
      get_document_size=store.size,
      retrieve_whole_document_content=store.get,
      get_number_of_chunks=store.chunk_count,
      get_document_chunk_by_index=store.get_chunk,
  )
  ```

- **Use `monkeypatch` for env vars** (not manual `os.environ`):
  ```python
  def test_x(monkeypatch):
      monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-...")
      ...
  ```
  `monkeypatch` reverts at the end of the test automatically.

- **Public API counts.** `test_package.py::test_package_metadata`
  asserts the exact size of `dpo_agent.__all__`. When you add a new
  name to the public API, update this number — and add the name
  with a brief comment explaining what it does.

- **Run tests from the project root:**
  ```bash
  cd ~/dpo-agent && /home/npinot/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/
  ```
  Use the hermes venv, not `python3` (which uses system Python).

## 5. Backward compatibility — IMPORTANT

The public API has accumulated **legacy aliases** that may not
match the canonical class names:

- `DPOAgent(...)` — factory returning `Agent(..., task="dpo")`
- `DPOAgentTwoPass(...)` — returns `AgentTwoPass(..., task="dpo")`
- `DPONavigator(...)` — returns `Navigator(..., task="dpo")`
- `DPOStreamingAgent(...)` — returns `StreamingAgent(..., task="dpo")`

These exist because the original `DPO*` classes were renamed to
`Agent*`/etc. when the package was generalized. **Don't delete
them** — external code may import them. If you grep for usages and
find none in `dpo_agent/`, **that's a coincidence** — the public
API of a Python package is its surface, even when internal code
doesn't use it.

The `client: anthropic.Anthropic` parameter to Agent/etc.
constructors is **also** a backward-compat surface. Existing tests
pass `MagicMock` objects with `__class__` set to a fake
`"Anthropic"`. The `_wrap_anthropic_client()` function detects
this via class name and wraps the legacy instance. Don't break
this — there are tests that depend on it.

## 6. Environment vars & config

The model selection is env-var-driven. The precedence is:

1. `DPO_AGENT_MODEL_{LOW|MEDIUM|HIGH}` — kind-specific
2. `LLM_MODEL` — legacy single-model override
3. Hardcoded default per kind

`dpo_agent/models.py` is the source of truth. `resolve_model("medium")`
returns the right model ID; `AgentConfig.model` defaults to
`resolve_model("medium")`.

In docker-compose, the web service has `env_file: .env` so the
`.env` file is loaded into the container. Don't change the
`environment:` block to use `${VAR:-}` defaults only — that reads
from the **shell**, not `.env`, and the user has `OPENROUTER_API_KEY`
in `.env` but no env-key set in their shell.

## 7. The web stack (4-service docker-compose)

The `docker-compose.yml` runs:

- **`web`** — FastAPI app via uvicorn, 4 workers, port 8000
  (internal)
- **`nginx`** — reverse proxy, port 80, with `proxy_buffering off`
  + `proxy_cache off` + `gzip off` in `/pipeline/stream` and
  `/review/stream` location blocks. **Do not** add gzip/buffering
  there — SSE requires unbuffered streaming.
- **`redis`** — pub/sub for SSE fan-out across multiple web workers
  (only used when `REDIS_URL` is set and `redis` package installed)
- **`neo4j`** — Neo4j Community, optional; falls back to SQLite if
  not configured

- **Endpoints:**

- `POST /review` — single-task DPO review (blocking)
- `POST /review/stream` — single-task SSE stream
- `POST /pipeline` — full 5-stage triage (blocking)
- `POST /pipeline/stream` — full 5-stage triage with SSE events
- `POST /contract/upload` — multipart file upload; returns
  extracted text as JSON. Supports `.pdf`, `.docx`, `.html`,
  `.htm`, `.md`, `.txt`, `.markdown`

SSE events: `stage_start`, `stage_complete`, `pipeline_complete`,
`error`. The web frontend JS (`dpo_agent/web/app.js`) handles each
in `handleEvent()`-style dispatch.

### 7a. Web Modes (bundled example vs. uploaded/pasted contract)

The frontend has two modes for contract input:

1. **Bundled example** — uses the `example-dpa` document bundled
   with the package (a small Data Processing Addendum).
2. **Paste/upload** — the user can either paste contract text
   into a textarea **or upload a file** via drag-and-drop or
   click-to-pick.

File upload routes via two paths:

- **Plain text formats** (`.md`, `.markdown`, `.txt`) are
  read by the browser via `FileReader.readAsText()` — no
  server round-trip, instant.
- **Binary formats** (`.pdf`, `.docx`, `.html`, `.htm`) are
  POSTed as multipart to `/contract/upload` for server-side
  parsing by `pdfplumber` / `python-docx` / `BeautifulSoup`.
  The extracted text (with `<!-- Page N -->` markers) fills
  the same textarea.

Both paths land in `#inline-text` and the existing
`PipelineRequest.inline_text` field carries the contract
text. The `setupUpload()` function in `app.js` dispatches
to the right path based on the file extension (the
`SERVER_PARSE_EXTS` set).

The browser enforces a 5 MB cap (visual warning in the
upload zone). The server enforces a 50 MB cap (returns 413
on oversize). Server-side errors (corrupt PDF, encrypted
DOCX) return 422 and surface as inline errors in the UI.

To add a new binary format: extend `SUPPORTED_EXTENSIONS` in
`dpo_agent/upload_extract.py`, add an `if ext == ".newfmt"`
branch in `extract_text()`, install the parser library, and
add it to `pyproject.toml`'s `dependencies`.

## 8. Common pitfalls

### SSE: `\n\n` vs `\\n\\n`

f-strings interpret `\n` as a newline. `\\n` in an f-string
source is the literal 2-char string `\n`, not a newline.
**Never** write `yield f"data: ...}\\n\\n"` — it must be
`yield f"data: ...}\n\n"`. The web frontend's SSE parser searches
for `\n\n` (real newlines) in the buffer. Literal `\n` (backslash +
n) gets parsed by `TextDecoder` as the literal characters `\n`,
and the parser silently drops every event. The whole pipeline
will appear "stuck" with no error.

Symptom: UI says "Running…" forever, server logs show 200 OK,
log panel shows "Starting pipeline…" but no other events.

### Anthropic SDK content blocks vs dataclasses

When you `import anthropic` directly and call
`client.messages.create(...)`, the response has `.content` as a
list of **Anthropic SDK content blocks** (with `.type`, `.text`,
`.name`, `.input` attributes). When you go through `LLMClient`,
the response has `.content` as a list of **`TextBlock`/`ToolUseBlock`
dataclasses**. Use `isinstance(block, TextBlock)` to check —
don't check `block.type == "text"` because dataclass blocks have
`.type` as a string field, which works but the dataclass-style
check is the convention.

When appending back to the `messages` list for the next LLM call,
use `_content_to_anthropic_dict(blocks)` (importable from
`dpo_agent.agent`) to convert the dataclasses back into the
Anthropic wire format the SDK expects.

### Prompt caching requires the list form of `system`

The `cache_system_prompt: True` config (default) sends system as:

```python
kwargs["system"] = [{
    "type": "text",
    "text": self.system_prompt,
    "cache_control": {"type": self.config.cache_ttl},
}]
```

If you set `cache_system_prompt=False`, the system collapses to a
single string. The Anthropic SDK accepts both; the OpenAI-compat
backend flattens the list form into one OpenAI system message.
**Don't change the cache_control field name** — Anthropic rejects
unknown fields.

### Backward compat: returning a raw `str` from `_call_model`

Don't. The Agent class expects a `LLMResponse` (with `.content`,
`.stop_reason`, `.usage`). Even mock test code must produce an
`LLMResponse` — use `dpo_agent.llm_client.LLMResponse(content=[...],
stop_reason="end_turn")`. The legacy code (pre-Path-D) returned
Anthropic SDK response objects; that path is gone.

### Streaming: tool dispatch happens AFTER the stream

If you're refactoring streaming code, **do not** put tool dispatch
inside the `for ev in stream:` loop — that was the Anthropic-SDK
pattern. The canonical LLMClient emits content_block_start /
content_block_delta / message_stop events but does **not** dispatch
tools inline. Instead, the LLMResponse is constructed when
`stream.get_final_message()` is called, and tool dispatch should
happen **after** that call by iterating `final_message.content`
for `ToolUseBlock` instances.

### dpo-agent's kgpipeline is NOT wiki-contracts/kgpipeline

There used to be an external package at `wiki-contracts/kgpipeline/`.
That is **deprecated**. The local `dpo_agent/kg/` module is the
canonical implementation. The Dockerfile no longer needs to vendor
`../wiki-contracts`. The `dpo_agent.integrations.kgpipeline` module
imports from `dpo_agent.kg`, not from the external package.

## 9. Where to look when adding a feature

| Task | Files to touch |
|---|---|
| Add a new task (e.g. "redact") | `dpo_agent/tasks/redact/{reviewer,critique,navigator}.md` — that's it. Auto-discovered. |
| Add a new prompt field to AgentConfig | `dpo_agent/agent.py::AgentConfig` + JSON resolution in `dpo_agent.cli` (if CLI) |
| Add a new env var | `dpo_agent/models.py` for kinds, `.env.example`, `docker-compose.yml` if it's a deploy-time var |
| Add a new LLM backend (e.g. Together) | `dpo_agent/llm_client.py` — subclass `LLMClient`, implement `create` and `stream`, register name in `create_client` factory |
| Add a new graph layer (e.g. deduplication) | `dpo_agent/kg/` for the deterministic Python + `dpo_agent/tasks/kg_<name>/` for the LLM-driven layer |
| Add a new API endpoint | `dpo_agent/examples/fastapi_server.py`, `dpo_agent/web/app.js` for the frontend |
| Change prompt format | `dpo_agent/tasks/<task>/reviewer.md` — restart the web container |
| Add a docker service | `docker-compose.yml`, `docker/` for nginx config |

## 10. Running tests

```bash
# All tests (~2 seconds, 408 pass)
cd ~/dpo-agent && /home/npinot/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/

# Single test file
cd ~/dpo-agent && /home/npinot/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/test_llm_client.py -v

# Single test
cd ~/dpo-agent && /home/npinot/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/test_llm_client.py::test_mock_client_create_returns_text_response

# With full traceback for a failure
cd ~/dpo-agent && /home/npinot/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/<file> --tb=long -x
```

## 11. Docker workflow

```bash
cd ~/dpo-agent

# Build the web image (cached; only rebuilds if Docker-relevant files change)
docker compose build web

# Start the stack (4 services)
docker compose up -d

# Tail logs from the web service
docker compose logs --tail=50 web

# Restart a single service (e.g. after changing nginx.conf)
docker compose restart nginx

# Tear down
docker compose down

# Run a one-off command inside the container (e.g. open a Python REPL)
docker exec -it dpo-agent-web python3
```

If you change a Python file inside `dpo_agent/`, you need
`docker compose build web && docker compose up -d` to pick it up
(the image is built, not bind-mounted).

If you change `docker/nginx.conf` or `docker-compose.yml`,
restart the affected service: `docker compose restart nginx` /
`docker compose up -d`.

## 12. Quick orientation for a fresh agent

If you've never seen this codebase:

1. Read the README.md (user-facing overview).
2. Run `pytest tests/` to see what passes (should be 408).
3. Read `dpo_agent/agent.py::Agent.run()` — this is the heart of
   the tool-use loop. The other 3 agent classes (Navigator,
   AgentTwoPass, StreamingAgent) are variations on this same loop.
4. Read `dpo_agent/llm_client.py` — the LLM backend abstraction.
5. Read one task's prompts end-to-end (e.g. `dpo_agent/tasks/dpo/`).
6. Look at `dpo_agent/examples/fastapi_server.py` to see how the
   pieces wire up to HTTP.

After that, you can read tasks/, kg/, and integrations/ as needed.
