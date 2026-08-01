// dpo-agent web UI — vanilla JS, no framework.
//
// Connects to the FastAPI server's /pipeline/stream endpoint via
// Server-Sent Events, renders a live progress display, and shows
// the final report.

(function () {
  "use strict";

  // ---- State ----
  const state = {
    serverOnline: false,
    pipelineRunning: false,
    currentStages: [], // [{task, status, elapsed_seconds, ...}]
    finalReport: null, // {markdown, json, ...}
    costThreshold: 5.0,
  };

  // ---- DOM helpers ----
  const $ = (id) => document.getElementById(id);
  const fmtTime = (s) => {
    if (s == null) return "—";
    return `${s.toFixed(1)}s`;
  };
  const fmtCost = (c) =>
    c == null ? "—" : `$${c.toFixed(3)}`;

  // ---- Markdown rendering (lightweight) ----
  // A small, dependency-free markdown-to-HTML converter.
  // Handles: headings, bold, italic, code, lists, tables.
  // Not a full CommonMark implementation, but covers the
  // triage-report output well enough for a UI.
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderMarkdown(md) {
    if (!md) return "";
    const lines = md.split("\n");
    const out = [];
    let i = 0;
    let inList = false;
    let inTable = false;
    let tableRows = [];

    function closeList() {
      if (inList) { out.push("</ul>"); inList = false; }
    }
    function closeTable() {
      if (inTable) {
        out.push(renderTable(tableRows));
        tableRows = [];
        inTable = false;
      }
    }
    function renderTable(rows) {
      if (rows.length < 2) {
        return rows.map(r => `<p>${r}</p>`).join("");
      }
      const header = rows[0];
      const align = rows[1];
      const body = rows.slice(2);
      const ths = header
        .split("|").map(s => s.trim())
        .filter((_, i, arr) => i > 0 && i < arr.length - 1 || (i === 0 && arr.length === 1))
        .map(s => `<th>${inline(s)}</th>`)
        .join("");
      const trs = body.map(row => {
        const tds = row.split("|").map(s => s.trim())
          .filter((_, i, arr) => i > 0 && i < arr.length - 1 || (i === 0 && arr.length === 1))
          .map(s => `<td>${inline(s)}</td>`)
          .join("");
        return `<tr>${tds}</tr>`;
      }).join("");
      return `<table><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
    }
    function inline(s) {
      return escapeHtml(s)
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\*([^*]+)\*/g, "<em>$1</em>")
        .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    }
    function flushPara(buf) {
      if (buf.length === 0) return;
      const text = buf.join(" ").trim();
      if (text) out.push(`<p>${inline(text)}</p>`);
      buf.length = 0;
    }

    const paraBuf = [];
    while (i < lines.length) {
      const line = lines[i];
      // Tables (| ... |)
      if (line.trim().startsWith("|") && line.trim().endsWith("|")) {
        closeList();
        flushPara(paraBuf);
        inTable = true;
        while (i < lines.length && lines[i].trim().startsWith("|") && lines[i].trim().endsWith("|")) {
          tableRows.push(lines[i]);
          i++;
        }
        closeTable();
        continue;
      }
      // Headings
      const h = line.match(/^(#{1,6})\s+(.+)$/);
      if (h) {
        closeList();
        flushPara(paraBuf);
        const level = h[1].length;
        out.push(`<h${level}>${inline(h[2])}</h${level}>`);
        i++;
        continue;
      }
      // Horizontal rule
      if (line.trim() === "---") {
        closeList();
        flushPara(paraBuf);
        out.push("<hr/>");
        i++;
        continue;
      }
      // Unordered list
      const ul = line.match(/^[-*]\s+(.+)$/);
      if (ul) {
        flushPara(paraBuf);
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push(`<li>${inline(ul[1])}</li>`);
        i++;
        continue;
      }
      // Empty line
      if (line.trim() === "") {
        closeList();
        flushPara(paraBuf);
        i++;
        continue;
      }
      // Regular paragraph text
      closeList();
      paraBuf.push(line.trim());
      i++;
    }
    closeList();
    flushPara(paraBuf);
    return out.join("\n");
  }

  // ---- Server health check ----
  async function checkServer() {
    const status = $("server-status");
    try {
      const r = await fetch("/healthz");
      if (r.ok) {
        state.serverOnline = true;
        status.innerHTML =
          '<span class="status-dot status-ok"></span>' +
          '<span class="status-text">Server online</span>';
      } else {
        throw new Error("not ok");
      }
    } catch (e) {
      state.serverOnline = false;
      status.innerHTML =
        '<span class="status-dot status-error"></span>' +
        '<span class="status-text">Server offline</span>';
    }
  }

  // ---- Mode toggle (example vs inline) ----
  function setupModeToggle() {
    const radios = document.querySelectorAll('input[name="mode"]');
    const docIdSection = $("document-id-section");
    const inlineSection = $("inline-section");
    const docIdInput = $("document-id");
    const inlineText = $("inline-text");

    radios.forEach(r => {
      r.addEventListener("change", () => {
        if (r.value === "example" && r.checked) {
          docIdSection.classList.remove("hidden");
          inlineSection.classList.add("hidden");
          docIdInput.value = "example-dpa";
        } else if (r.value === "inline" && r.checked) {
          docIdSection.classList.remove("hidden");
          inlineSection.classList.add("hidden");
          docIdInput.value = "inline-contract";
        }
      });
    });

    // Use a "Paste contract" button to actually show the textarea
    // (kept hidden by default for the example).
    docIdSection.insertAdjacentHTML("beforeend",
      '<button class="small-button" id="toggle-inline" style="margin-top:6px">Paste contract text instead</button>');
    $("toggle-inline").addEventListener("click", () => {
      inlineSection.classList.toggle("hidden");
      if (!inlineSection.classList.contains("hidden")) {
        docIdInput.value = "inline-contract";
      }
    });
  }

  // ---- Tab switching ----
  function setupTabs() {
    document.querySelectorAll(".tab").forEach(tab => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
        document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
        tab.classList.add("active");
        const target = tab.dataset.tab;
        $(`${target}-tab`).classList.add("active");
      });
    });
  }

  // ---- Pipeline run ----
  function setStageStatus(idx, status, info) {
    const stageEl = document.querySelector(`.stage[data-stage="${idx}"]`);
    if (!stageEl) return;
    stageEl.classList.remove("stage-pending", "stage-running",
                            "stage-complete", "stage-error");
    stageEl.classList.add(`stage-${status}`);

    const statusEl = stageEl.querySelector(".stage-status");
    const progressEl = stageEl.querySelector(".stage-progress");
    if (status === "running") {
      statusEl.textContent = "Running…";
      progressEl.textContent = "";
    } else if (status === "complete" && info) {
      statusEl.textContent = "Complete";
      progressEl.textContent =
        `${fmtTime(info.elapsed_seconds)} · ${info.tool_calls || 0} calls`;
    } else if (status === "error") {
      statusEl.textContent = "Error";
      progressEl.textContent = info && info.error ? info.error : "";
    } else {
      statusEl.textContent = "Pending";
      progressEl.textContent = "";
    }
  }

  function logEvent(stage, message, type) {
    const log = $("live-log");
    if (log.querySelector(".log-empty")) log.innerHTML = "";
    const time = new Date().toLocaleTimeString();
    const line = document.createElement("div");
    line.className = `log-line log-${type || "info"}`;
    line.innerHTML =
      `<span class="log-time">${time}</span>` +
      `<span class="log-stage">${stage || "—"}</span>` +
      `<span class="log-message">${escapeHtml(message || "")}</span>`;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  }

  function resetUI() {
    state.currentStages = [];
    state.finalReport = null;
    // Close any per-stage SSE streams from a prior run.
    closeAllStageStreams();
    document.querySelectorAll(".stage").forEach(s => {
      s.classList.remove("stage-running", "stage-complete", "stage-error");
      s.classList.add("stage-pending");
      s.querySelector(".stage-status").textContent = "Pending";
      s.querySelector(".stage-progress").textContent = "";
      // Clear the per-stage event log if the panel is open.
      const logEl = s.querySelector(".stage-event-log");
      if (logEl) {
        logEl.innerHTML =
          '<p class="log-empty">Click ▶ to start streaming this task.</p>';
      }
    });
    $("live-log").innerHTML =
      '<p class="log-empty">Click "Run triage pipeline" to start.</p>';
    $("report-content").classList.add("hidden");
    $("report-empty").classList.remove("hidden");
    $("json-content").classList.add("hidden");
    $("json-empty").classList.remove("hidden");
    $("pipeline-title").textContent = "Running…";
    $("pipeline-meta").textContent = "";
  }

  async function runPipeline() {
    if (state.pipelineRunning) return;
    if (!state.serverOnline) {
      await checkServer();
      if (!state.serverOnline) {
        alert("Server is offline. Start the FastAPI server first:\n\n" +
              "uvicorn dpo_agent.examples.fastapi_server:app --reload");
        return;
      }
    }

    state.pipelineRunning = true;
    $("run-button").classList.add("hidden");
    $("cancel-button").classList.remove("hidden");
    resetUI();

    // Build the request body.
    const useInline = !$("inline-section").classList.contains("hidden");
    const body = {
      document_id: $("document-id").value,
      jurisdiction_notes: $("jurisdiction").value,
    };
    if (useInline && $("inline-text").value.trim()) {
      body.inline_text = $("inline-text").value;
    }

    logEvent("pipeline", "Starting pipeline…", "info");

    // EventSource doesn't support POST; use fetch with
    // streaming response and a manual SSE parser.
    try {
      const r = await fetch("/pipeline/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        throw new Error(`Server returned ${r.status}`);
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let stageIndex = -1;

      while (true) {
        const { value, done } = await reader.read();
        // Process the value FIRST, then check done. Otherwise
        // the last chunk of events gets dropped when the stream
        // closes cleanly (the reader returns done: true with the
        // last chunk, but if we break before processing, the
        // events are lost). This was the bug that caused the
        // UI to stay "Running…" forever even though the server
        // sent the error event correctly.
        if (value) {
          buffer += decoder.decode(value, { stream: true });

          // SSE events are separated by "\n\n". Parse each one.
          let sep;
          while ((sep = buffer.indexOf("\n\n")) !== -1) {
            const raw = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            // Each event is "data: <json>\n" (or multi-line).
            const lines = raw.split("\n");
            for (const ln of lines) {
              if (!ln.startsWith("data:")) continue;
              const payload = ln.slice(5).trim();
              if (!payload) continue;
              try {
                const event = JSON.parse(payload);
                handleEvent(event);
                if (event.type === "stage_start") {
                  stageIndex++;
                  setStageStatus(stageIndex, "running");
                  logEvent(event.task, "Started", "info");
                } else if (event.type === "stage_complete") {
                  const idx = document.querySelectorAll(".stage")
                    .length; // compute actual index
                  // Find the running stage
                  const running = document.querySelector(".stage-running");
                  if (running) {
                    const idx = parseInt(running.dataset.stage);
                    setStageStatus(idx, event.succeeded ? "complete" : "error", event);
                    logEvent(event.task,
                      event.succeeded
                        ? `Complete (${fmtTime(event.elapsed_seconds)}, ${event.tool_calls} calls)`
                        : `Error: ${event.error || "unknown"}`,
                      event.succeeded ? "success" : "error");
                  }
                } else if (event.type === "pipeline_complete") {
                  state.finalReport = event;
                  $("pipeline-title").textContent =
                    `Triage complete: ${event.document_id}`;
                  $("pipeline-meta").textContent =
                    `${fmtTime(event.total_elapsed_seconds)} total · ${fmtCost(event.total_cost_estimate)} estimated`;
                  renderReport(event);
                  logEvent("pipeline", "All stages complete", "success");
                } else if (event.type === "error") {
                  logEvent("error", event.error, "error");
                  // Also surface the error in the status bar and the
                  // currently-running stage so the user sees that
                  // the pipeline didn't silently disappear. Without
                  // this, the UI is stuck on "Running…" and
                  // "Pending" forever, even though the server has
                  // already errored out.
                  $("pipeline-title").textContent = "Pipeline errored";
                  const running = document.querySelector(".stage-running");
                  if (running) {
                    setStageStatus(parseInt(running.dataset.stage), "error", event);
                  } else {
                    // No running stage (e.g. errored in the SSE
                    // plumbing itself, before any stage started).
                    // Mark stage 0 as errored.
                    setStageStatus(0, "error", event);
                  }
                  $("pipeline-meta").textContent = event.error || "Unknown error";
                }
              } catch (e) {
                console.warn("Failed to parse SSE event:", payload, e);
              }
            }
          }
        }
        if (done) break;
      }
    } catch (e) {
      logEvent("error", e.message, "error");
      $("pipeline-title").textContent = "Error";
    } finally {
      state.pipelineRunning = false;
      $("run-button").classList.remove("hidden");
      $("cancel-button").classList.add("hidden");
    }
  }

  function handleEvent(event) {
    // Switch on event type for any per-event logic.
  }

  function renderReport(report) {
    // Render markdown.
    const reportRendered = $("report-rendered");
    reportRendered.innerHTML = renderMarkdown(report.markdown);
    $("report-empty").classList.add("hidden");
    $("report-content").classList.remove("hidden");

    // Render JSON.
    $("json-rendered").textContent =
      JSON.stringify(report.json, null, 2);
    $("json-empty").classList.add("hidden");
    $("json-content").classList.remove("hidden");

    // Switch to the report tab.
    document.querySelector('.tab[data-tab="report"]').click();
  }

  // ---- Copy / Download ----
  function setupCopyDownload() {
    $("copy-report").addEventListener("click", () => {
      if (!state.finalReport) return;
      navigator.clipboard.writeText(state.finalReport.markdown);
      logEvent("ui", "Report copied to clipboard", "info");
    });
    $("download-report").addEventListener("click", () => {
      if (!state.finalReport) return;
      downloadFile("triage-report.md",
        state.finalReport.markdown, "text/markdown");
    });
    $("copy-json").addEventListener("click", () => {
      if (!state.finalReport) return;
      navigator.clipboard.writeText(
        JSON.stringify(state.finalReport.json, null, 2));
      logEvent("ui", "JSON copied to clipboard", "info");
    });
    $("download-json").addEventListener("click", () => {
      if (!state.finalReport) return;
      downloadFile("triage-report.json",
        JSON.stringify(state.finalReport.json, null, 2),
        "application/json");
    });
  }

  function downloadFile(name, content, mime) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  }

  // ---- File upload (markdown / text contracts) ----
  // Browser-side: read a file and put its contents in the
  // existing #inline-text textarea. The server already accepts
  // the contract text via the PipelineRequest.inline_text field,
  // so the rest of the pipeline path is unchanged.
  const UPLOAD_MAX_BYTES = 5 * 1024 * 1024;  // 5 MB cap
  // All extensions accepted by the file input. Text formats
  // can be read with FileReader; PDF/DOCX must POST to the
  // /contract/upload endpoint for server-side parsing.
  const UPLOAD_ACCEPT_EXTS = new Set([
    ".md", ".markdown", ".txt",
    ".pdf", ".docx",
    ".html", ".htm",
  ]);
  // Extensions routed through /contract/upload. Plain text
  // formats skip the round-trip and read via FileReader.
  const SERVER_PARSE_EXTS = new Set([".pdf", ".docx", ".html", ".htm"]);

  // ---- Per-stage expand → SSE event stream ----
  //
  // Each stage in the progress panel has an expand chevron.
  // When the user clicks it, a hidden panel slides open below
  // the stage row and connects to `/review/stream` for that
  // task. The streamed events (tool calls, text chunks,
  // sections) are rendered as they arrive.
  //
  // Cost-control: this only happens for stages the user has
  // explicitly expanded. Collapsed stages don't trigger any
  // streaming. If the user clicks Run Pipeline while a stage
  // is expanded, the per-stage stream runs IN PARALLEL with
  // the pipeline's own SSE — both feeds are visible.

  // Per-stage SSE state. Keyed by stage idx. Each entry
  // holds the active abort controller so we can close it on
  // collapse or on a new run.
  const stageStreams = new Map(); // idx -> { controller }

  function setupStageExpand() {
    document.querySelectorAll(".stage-expand").forEach((btn) => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.getAttribute("data-expand"));
        const stageEl = document.querySelector(
          `.stage[data-stage="${idx}"]`
        );
        if (!stageEl) return;
        const streamEl = stageEl.querySelector(
          `.stage-event-stream[data-stream="${idx}"]`
        );
        const isOpen = !streamEl.hidden;
        if (isOpen) {
          // Collapse: hide the panel and stop the SSE.
          streamEl.hidden = true;
          stageEl.classList.remove("stage-expanded");
          closeStageStream(idx);
        } else {
          // Expand: show the panel and start the SSE.
          streamEl.hidden = false;
          stageEl.classList.add("stage-expanded");
          openStageStream(idx);
        }
      });
    });
  }

  async function openStageStream(idx) {
    if (stageStreams.has(idx)) return;

    const stageEl = document.querySelector(
      `.stage[data-stage="${idx}"]`
    );
    const taskName = stageEl ? stageEl.getAttribute("data-task") : null;
    if (!taskName) return;

    const useInline = !$("inline-section").classList.contains("hidden");
    const body = {
      task: taskName,
      document_id: $("document-id").value,
      jurisdiction_notes: $("jurisdiction").value,
    };
    if (useInline && $("inline-text").value.trim()) {
      body.inline_text = $("inline-text").value;
    }

    const controller = new AbortController();
    stageStreams.set(idx, { controller });

    const logEl = document.querySelector(
      `.stage-event-log[data-log="${idx}"]`
    );
    if (logEl) {
      logEl.innerHTML = "";
      appendEventLog(idx, { type: "connecting", agent: taskName });
    }

    try {
      const r = await fetch("/review/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!r.ok) {
        appendEventLog(idx, {
          type: "agent_error",
          message: `Server returned ${r.status}`,
        });
        return;
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (value) {
          buffer += decoder.decode(value, { stream: true });
          let sep;
          while ((sep = buffer.indexOf("\n\n")) !== -1) {
            const raw = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            for (const ln of raw.split("\n")) {
              if (!ln.startsWith("data:")) continue;
              const payload = ln.slice(5).trim();
              if (!payload) continue;
              try {
                const event = JSON.parse(payload);
                renderStageEvent(idx, event);
              } catch (_e) {
                /* skip malformed */
              }
            }
          }
        }
        if (done) break;
      }
    } catch (e) {
      if (e && e.name !== "AbortError") {
        appendEventLog(idx, {
          type: "agent_error",
          message: String(e.message || e),
        });
      }
    } finally {
      stageStreams.delete(idx);
    }
  }

  function closeStageStream(idx) {
    const entry = stageStreams.get(idx);
    if (entry && entry.controller) {
      try { entry.controller.abort(); } catch (_e) { /* ignore */ }
    }
    stageStreams.delete(idx);
  }

  function closeAllStageStreams() {
    for (const idx of Array.from(stageStreams.keys())) {
      closeStageStream(idx);
    }
  }

  function renderStageEvent(idx, event) {
    const logEl = document.querySelector(
      `.stage-event-log[data-log="${idx}"]`
    );
    if (!logEl) return;
    // Trim the leading "Connecting…" line on the first real
    // event so the log doesn't start with a stale placeholder.
    if (
      logEl.children.length === 1 &&
      logEl.firstChild.classList.contains("event-line-connecting")
    ) {
      logEl.innerHTML = "";
    }
    appendEventLog(idx, event);
  }

  function appendEventLog(idx, event) {
    const logEl = document.querySelector(
      `.stage-event-log[data-log="${idx}"]`
    );
    if (!logEl) return;
    const line = document.createElement("div");
    line.className = "event-line";
    if (event.type === "connecting") {
      line.classList.add("event-line-connecting");
    }

    const typeBadge = document.createElement("span");
    typeBadge.className = `event-type ${event.type}`;
    typeBadge.textContent = event.type;
    line.appendChild(typeBadge);

    // Per-event-type detail rendering.
    if (event.type === "agent_start") {
      const meta = document.createElement("span");
      meta.className = "event-meta";
      meta.textContent =
        `agent=${event.agent || "?"}, doc=${event.document_id || "?"}`;
      line.appendChild(meta);
    } else if (event.type === "tool_call_start") {
      const tool = document.createElement("span");
      tool.className = "event-tool-name";
      tool.textContent = event.name || "(tool)";
      line.appendChild(tool);
      if (event.input) {
        const inp = document.createElement("div");
        inp.className = "event-tool-input";
        inp.textContent = formatToolInput(event.input);
        line.appendChild(inp);
      }
    } else if (event.type === "tool_call_complete") {
      const tool = document.createElement("span");
      tool.className = "event-tool-name";
      tool.textContent = event.name || "(tool)";
      line.appendChild(tool);
      if (event.output_preview) {
        const out = document.createElement("div");
        out.className = "event-tool-input";
        out.textContent = `↳ ${String(event.output_preview).slice(0, 200)}`;
        line.appendChild(out);
      }
    } else if (event.type === "text_chunk") {
      const text = document.createElement("span");
      text.textContent = event.text || "";
      line.appendChild(text);
    } else if (event.type === "section_complete") {
      const head = document.createElement("span");
      head.textContent = event.heading || "(section)";
      line.appendChild(head);
    } else if (event.type === "agent_complete") {
      const meta = document.createElement("span");
      meta.className = "event-meta";
      meta.textContent =
        `tokens: in=${event.input_tokens ?? "?"}, ` +
        `out=${event.output_tokens ?? "?"}`;
      line.appendChild(meta);
    } else if (event.type === "agent_error") {
      const msg = document.createElement("span");
      msg.style.color = "var(--error)";
      // StreamingAgent emits either `message` (LLMClient) or
      // `error` (StreamingAgent's two-pass code). Prefer message;
      // fall back to error; fall back to a full JSON dump.
      msg.textContent =
        event.message ||
        event.error ||
        JSON.stringify(event);
      line.appendChild(msg);
    } else if (event.type === "connecting") {
      const msg = document.createElement("span");
      msg.className = "event-meta";
      msg.textContent =
        `Connecting to /review/stream for task=${event.agent}…`;
      line.appendChild(msg);
    } else {
      const txt = document.createElement("span");
      txt.className = "event-meta";
      txt.textContent = JSON.stringify(event).slice(0, 200);
      line.appendChild(txt);
    }

    logEl.appendChild(line);
    // Auto-scroll to the bottom.
    const streamEl = logEl.closest(".stage-event-stream");
    if (streamEl) streamEl.scrollTop = streamEl.scrollHeight;
  }

  function formatToolInput(input) {
    if (typeof input === "string") return input;
    try {
      return JSON.stringify(input, null, 0).slice(0, 300);
    } catch (_e) {
      return String(input).slice(0, 300);
    }
  }

  /**
   * Sets up the file input, drag-drop zone, and clear button.
   * Called once during init().
   */
  function setupUpload() {
    const fileInput = $("file-input");
    const uploadZone = $("upload-zone");
    const uploadedFile = $("uploaded-file");
    const uploadedFileName = $("uploaded-file-name");
    const uploadedFileSize = $("uploaded-file-size");
    const uploadedFileClear = $("uploaded-file-clear");
    const inlineSection = $("inline-section");
    const inlineText = $("inline-text");

    /** Show the inline section + drop the text in the textarea. */
    function applyFileToTextarea(filename, text) {
      // Make sure the inline section is visible so the user sees
      // their uploaded text and can edit it.
      inlineSection.classList.remove("hidden");
      inlineText.value = text;
      uploadedFileName.textContent = filename;
      uploadedFileSize.textContent = formatBytes(new Blob([text]).size);
      uploadedFile.classList.remove("hidden");
      // Switch document-id to a slug based on the filename so
      // the audit log shows something meaningful.
      const docIdInput = $("document-id");
      docIdInput.value = filename.replace(/\.[^.]+$/, "").replace(
        /[^a-zA-Z0-9_-]/g, "-"
      ).slice(0, 64) || "uploaded-contract";
      // Reset the file input so picking the same file twice
      // triggers a new change event.
      fileInput.value = "";
    }

    function showUploadError(message) {
      uploadZone.classList.add("upload-zone-error");
      const existing = uploadZone.querySelector(".upload-error");
      if (existing) existing.remove();
      const err = document.createElement("div");
      err.className = "upload-error";
      err.textContent = message;
      uploadZone.appendChild(err);
      setTimeout(() => err.remove(), 5000);
    }

    function clearUpload() {
      inlineText.value = "";
      uploadedFile.classList.add("hidden");
      uploadedFileName.textContent = "";
      uploadedFileSize.textContent = "";
      uploadZone.classList.remove("upload-zone-error");
      uploadZone.querySelectorAll(".upload-error").forEach(
        n => n.remove()
      );
    }

    /**
     * Validate a File object, then either read it (text
     * formats) or POST it to /contract/upload (PDF/DOCX/HTML)
     * for server-side parsing. Either way the result lands
     * in the #inline-text textarea. Errors surface via the
     * upload-zone-error style.
     */
    function handleFile(file) {
      if (!file) return;

      // Validation. Extension + size. We check the extension
      // rather than just MIME because browsers disagree on
      // text/markdown vs text/plain for .md.
      const lowerName = file.name.toLowerCase();
      const ext = lowerName.match(/\.[^.]+$/);
      if (!ext || !UPLOAD_ACCEPT_EXTS.has(ext[0])) {
        showUploadError(
          `Unsupported file type: ${file.name}. ` +
          `Use .md, .markdown, .txt, .pdf, .docx, .html, or .htm.`
        );
        return;
      }
      if (file.size > UPLOAD_MAX_BYTES) {
        showUploadError(
          `File too large: ${formatBytes(file.size)} (limit is ` +
          `${formatBytes(UPLOAD_MAX_BYTES)}).`
        );
        return;
      }

      if (SERVER_PARSE_EXTS.has(ext[0])) {
        handleServerParsedFile(file);
      } else {
        handleTextFile(file);
      }
    }

    /**
     * Read a plain-text file (md/txt/markdown) directly via
     * FileReader — no server round-trip.
     */
    function handleTextFile(file) {
      const reader = new FileReader();
      reader.onload = () => {
        try {
          const text = String(reader.result || "");
          if (!text.trim()) {
            showUploadError(`File is empty.`);
            return;
          }
          applyFileToTextarea(file.name, text);
          logEvent("upload", `Loaded ${file.name} (${formatBytes(file.size)})`, "info");
        } catch (e) {
          showUploadError(`Failed to read file: ${e.message}`);
        }
      };
      reader.onerror = () => {
        showUploadError(`Failed to read file: ${reader.error || "unknown error"}`);
      };
      reader.readAsText(file);
    }

    /**
     * POST a binary file (PDF/DOCX/HTML) to /contract/upload.
     * Server-side pdfplumber/python-docx parse it and return
     * extracted text as JSON. We then populate the textarea
     * with the result.
     */
    function handleServerParsedFile(file) {
      // Show a spinner-style "uploading…" message instead of
      // leaving the user guessing what happened during parsing.
      uploadZone.classList.add("upload-zone-uploading");
      const uploading = document.createElement("div");
      uploading.className = "upload-status";
      uploading.id = "upload-status-pending";
      uploading.textContent = `Uploading ${file.name}…`;
      uploadZone.appendChild(uploading);

      const formData = new FormData();
      formData.append("file", file, file.name);

      fetch("/contract/upload", {
        method: "POST",
        body: formData,
      })
        .then(async (r) => {
          // Always tear down the pending status, regardless
          // of whether the upload succeeded or failed.
          const pending = document.getElementById(
            "upload-status-pending"
          );
          if (pending) pending.remove();
          uploadZone.classList.remove("upload-zone-uploading");

          if (!r.ok) {
            // Try to parse the JSON error from FastAPI.
            let detail = `Server returned ${r.status}`;
            try {
              const body = await r.json();
              if (body && body.detail) {
                if (typeof body.detail === "string") {
                  detail = body.detail;
                } else if (body.detail && body.detail.message) {
                  detail = body.detail.message;
                  if (body.detail.supported_formats) {
                    detail += ` Supported: ${
                      body.detail.supported_formats.join(", ")
                    }`;
                  }
                }
              }
            } catch (_e) {
              // Body wasn't JSON — keep the status-code message.
            }
            showUploadError(detail);
            return;
          }
          return r.json();
        })
        .then((data) => {
          if (!data) return;
          const text = data.text || "";
          if (!text.trim()) {
            showUploadError(
              `Server returned empty text for ${file.name}. ` +
              `The file may be scanned/image-based.`
            );
            return;
          }
          applyFileToTextarea(file.name, text);
          logEvent(
            "upload",
            `Parsed ${file.name} (${formatBytes(file.size)} ` +
            `→ ${formatBytes(data.char_count)} text, ${data.format})`,
            "info"
          );
        })
        .catch((err) => {
          // Network failure (offline, DNS, etc.) — not a server
          // response.
          showUploadError(
            `Network error uploading ${file.name}: ${err.message || err}`
          );
        });

      // Reset the file input so picking the same file twice
      // triggers a fresh change event.
      fileInput.value = "";
    }

    // File input change (click-to-pick).
    fileInput.addEventListener("change", () => {
      if (fileInput.files && fileInput.files[0]) {
        handleFile(fileInput.files[0]);
      }
    });

    // Drag-and-drop on the entire upload zone.
    uploadZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      uploadZone.classList.add("upload-zone-drag-over");
    });
    uploadZone.addEventListener("dragleave", (e) => {
      // Only remove the highlight if we left the zone, not a
      // child element (dragleave fires for every child too).
      if (e.target === uploadZone || !uploadZone.contains(e.relatedTarget)) {
        uploadZone.classList.remove("upload-zone-drag-over");
      }
    });
    uploadZone.addEventListener("drop", (e) => {
      e.preventDefault();
      uploadZone.classList.remove("upload-zone-drag-over");
      const file = e.dataTransfer && e.dataTransfer.files
        && e.dataTransfer.files[0];
      if (file) handleFile(file);
    });

    // Clear button.
    uploadedFileClear.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      clearUpload();
    });

    /** Human-readable byte / KB / MB. */
    function formatBytes(n) {
      if (n < 1024) return `${n} B`;
      if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
      return `${(n / (1024 * 1024)).toFixed(2)} MB`;
    }
  }

  // ---- Init ----
  function init() {
    setupModeToggle();
    setupStageExpand();
    setupUpload();
    setupTabs();
    setupCopyDownload();
    $("run-button").addEventListener("click", runPipeline);
    $("cancel-button").addEventListener("click", () => {
      // Cancelling mid-SSE is awkward without AbortController.
      // For now, just refresh the page.
      if (state.pipelineRunning) {
        if (confirm("Cancel the running pipeline? The page will reload.")) {
          location.reload();
        }
      }
    });
    checkServer();
    // Refresh server status every 30s.
    setInterval(checkServer, 30000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
