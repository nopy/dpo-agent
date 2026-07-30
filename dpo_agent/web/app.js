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
    document.querySelectorAll(".stage").forEach(s => {
      s.classList.remove("stage-running", "stage-complete", "stage-error");
      s.classList.add("stage-pending");
      s.querySelector(".stage-status").textContent = "Pending";
      s.querySelector(".stage-progress").textContent = "";
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
        if (done) break;
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
              }
            } catch (e) {
              console.warn("Failed to parse SSE event:", payload, e);
            }
          }
        }
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

  // ---- Init ----
  function init() {
    setupModeToggle();
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
