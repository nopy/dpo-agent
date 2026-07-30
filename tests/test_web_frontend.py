"""Tests for the streaming pipeline + web frontend.

The new endpoints are:
- POST /pipeline/stream — full 5-task triage with SSE
- POST /pipeline — non-streaming triage, returns JSON
- GET /, /styles.css, /app.js — static files for the web UI

We don't make any real API calls (no ANTHROPIC_API_KEY in
CI). Tests verify the wiring, the endpoint shapes, and the
frontend file structure.
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from dpo_agent.examples.fastapi_server import app


@pytest.fixture
def client():
    """A FastAPI test client."""
    return TestClient(app)


# ---- Static frontend files ----

def test_web_directory_exists():
    """The web/ directory should exist with index.html, styles.css, app.js."""
    web_dir = Path(__file__).parent.parent / "dpo_agent" / "web"
    assert web_dir.is_dir()
    assert (web_dir / "index.html").is_file()
    assert (web_dir / "styles.css").is_file()
    assert (web_dir / "app.js").is_file()


def test_index_html_has_required_elements():
    """The index.html should have the form elements the
    frontend JS expects."""
    web_dir = Path(__file__).parent.parent / "dpo_agent" / "web"
    html = (web_dir / "index.html").read_text()
    # Form elements
    assert 'id="document-id"' in html
    assert 'id="inline-text"' in html
    assert 'id="jurisdiction"' in html
    assert 'id="run-button"' in html
    assert 'id="cancel-button"' in html
    # Stage list
    assert 'data-stage="0"' in html
    assert 'data-stage="4"' in html
    # Tabs
    assert 'data-tab="progress"' in html
    assert 'data-tab="report"' in html
    assert 'data-tab="json"' in html
    # Output render targets
    assert 'id="report-rendered"' in html
    assert 'id="json-rendered"' in html
    # Stage names — the 5 default tasks
    assert "summarize" in html
    assert "clause_classification" in html
    assert "obligations" in html
    assert "risk_score" in html
    assert "dpo" in html


def test_app_js_uses_sse_protocol():
    """The app.js should connect to /pipeline/stream and parse
    SSE events (the stream from the FastAPI endpoint)."""
    web_dir = Path(__file__).parent.parent / "dpo_agent" / "web"
    js = (web_dir / "app.js").read_text()
    assert "/pipeline/stream" in js
    assert "fetch" in js
    # SSE parsing: events are separated by \n\n
    assert "data:" in js
    # Event types the backend emits
    assert "stage_start" in js
    assert "stage_complete" in js
    assert "pipeline_complete" in js
    assert "error" in js


def test_index_html_has_5_default_stages():
    """The index.html's stage list should reference all 5
    default pipeline tasks (the JS populates them dynamically
    from SSE events; the HTML provides the initial display)."""
    web_dir = Path(__file__).parent.parent / "dpo_agent" / "web"
    html = (web_dir / "index.html").read_text()
    for stage in ("summarize", "clause_classification", "obligations",
                  "risk_score", "dpo"):
        assert stage in html, f"missing {stage}"


def test_app_js_handles_pipeline_events():
    """The app.js should handle the 4 SSE event types the
    backend emits."""
    web_dir = Path(__file__).parent.parent / "dpo_agent" / "web"
    js = (web_dir / "app.js").read_text()
    for event_type in ("stage_start", "stage_complete",
                        "pipeline_complete", "error"):
        assert event_type in js, f"missing {event_type}"


def test_styles_css_has_stage_classes():
    """The styles.css should have the stage state classes
    (pending, running, complete, error)."""
    web_dir = Path(__file__).parent.parent / "dpo_agent" / "web"
    css = (web_dir / "styles.css").read_text()
    for cls in (".stage-pending", ".stage-running", ".stage-complete",
                ".stage-error"):
        assert cls in css, f"missing {cls}"


# ---- Static file serving (via the FastAPI app) ----

def test_serves_index_at_root(client):
    """GET / should return the index.html content."""
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "dpo-agent" in r.text


def test_serves_styles_css(client):
    """GET /styles.css should return the CSS with the right
    content type."""
    r = client.get("/styles.css")
    assert r.status_code == 200
    assert "text/css" in r.headers.get("content-type", "")
    assert "--accent" in r.text


def test_serves_app_js(client):
    """GET /app.js should return the JS with the right content type."""
    r = client.get("/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers.get("content-type", "")
    assert "fetch" in r.text


def test_healthz_endpoint(client):
    """GET /healthz should return OK JSON."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---- Pipeline endpoint shape ----

def test_pipeline_endpoint_validates_request(client):
    """POST /pipeline without a document_id should return 422."""
    r = client.post("/pipeline", json={})
    assert r.status_code == 422


def test_pipeline_stream_endpoint_validates_request(client):
    """POST /pipeline/stream without a document_id should
    return 422."""
    r = client.post("/pipeline/stream", json={})
    assert r.status_code == 422


@pytest.mark.xfail(reason="Requires ANTHROPIC_API_KEY to fully run",
                   strict=False)
def test_pipeline_request_schema_accepts_inline_text(client):
    """The pipeline endpoints should accept inline_text as
    an alternative to a stored document_id.

    This test requires an ANTHROPIC_API_KEY to actually run
    the pipeline; without one, the TypeError from the
    anthropic client propagates through the TestClient.
    Marked xfail so the test doesn't fail in CI but the
    intent is documented.
    """
    try:
        r = client.post(
            "/pipeline",
            json={
                "document_id": "test",
                "inline_text": "Test contract content. Provider shall...",
            },
        )
    except TypeError:
        pytest.xfail("Pipeline raised TypeError (no API key)")

    # We expect this to fail (no API key) but not with 422
    # (request validation passed). Accept any 4xx or 5xx
    # except 422.
    assert r.status_code != 422, f"validation failed: {r.text[:200]}"
    assert r.status_code >= 400, f"expected error, got {r.status_code}"


@pytest.mark.xfail(reason="Requires ANTHROPIC_API_KEY to fully run",
                   strict=False)
def test_pipeline_stream_yields_sse_events(client):
    """POST /pipeline/stream should return a streaming
    text/event-stream response with stage_start events."""
    r = client.post(
        "/pipeline/stream",
        json={
            "document_id": "test",
            "inline_text": "Test contract content. Provider shall...",
        },
    )
    # We expect this to fail eventually (no API key), but the
    # initial response should be a 200 with the SSE content type.
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")

    # Read the first few events. Don't drain the whole stream
    # because the pipeline will hang on the missing API key.
    content = b""
    for chunk in r.iter_bytes():
        content += chunk
        if b"stage_start" in content and len(content) > 200:
            break
        if len(content) > 5000:
            break

    text = content.decode("utf-8", errors="replace")
    # The first events should be stage_start for each of the
    # 5 default tasks.
    assert "stage_start" in text
    assert "summarize" in text
    assert "clause_classification" in text
    assert "obligations" in text
    assert "risk_score" in text
    assert "dpo" in text

    # Close the response to stop the pipeline thread.
    r.close()


# ---- Pipeline import sanity ----

def test_pipeline_request_schema_has_required_fields():
    """The PipelineRequest should have document_id required."""
    from dpo_agent.examples.fastapi_server import PipelineRequest
    fields = PipelineRequest.model_fields
    assert "document_id" in fields
    # document_id is required
    assert fields["document_id"].is_required()
    # jurisdiction_notes and inline_text are optional
    assert "jurisdiction_notes" in fields
    assert "inline_text" in fields


def test_default_tools_includes_example_dpa():
    """The default tools builder should include the example-dpa
    document (used by the web frontend's 'bundled example' mode)."""
    from dpo_agent.examples.fastapi_server import build_default_tools
    tools = build_default_tools()
    # The frontend relies on this document being available.
    # We can't easily test the full chain without an API key,
    # but we can verify the tools are built and the store has
    # the expected document.
    assert tools.get_document_size("example-dpa") > 0
