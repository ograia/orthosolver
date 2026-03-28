const state = {
  template: null,
  configFieldSpecs: [],
  currentProblemId: null,
  snapshot: null,
  progress: null,
  costSummary: null,
  llmUsage: null,
  problemInput: null,
  artifacts: [],
  requestArtifacts: [],
  requestLog: [],
  currentPage: "submit",
  selectedTreeNodeId: null,
  selectedTreeDetailTab: "proof",
  selectedRootTrackId: "all",
  leanFileCache: {},
  selectedRequestEntryId: null,
  selectedRequestArtifactKey: null,
  requestInspector: null,
  requestArtifactContentCache: {},
  selectedArtifactKey: null,
  sse: null,
  autoRunTimer: null,
  liveRefreshTimer: null,
  nextReconcileAtMs: 0,
  controlInFlight: false,
  tickInFlight: false,
  pauseHold: false,
  refreshScheduled: false,
  eventsLimit: 250,
};
const AGENT_KEYS = ["agent1", "agent2", "agent3", "agent4", "agent5", "agent6", "agent7", "agent8"];
const FIRST_ATTEMPT_AGENT_SPECS = [
  { key: "agent2-first-root", llmKey: "agent2_first_root", label: "agent2 first root" },
  { key: "agent2-first-lemma", llmKey: "agent2_first_lemma", label: "agent2 first lemma" },
  { key: "agent4-first", llmKey: "agent4_first", label: "agent4 first" },
];
const REQUEST_CONSOLE_HIDDEN_SOURCES = new Set(["api_create", "api_start", "api_pause", "api_resume", "api_run"]);
const MODEL_OPTIONS = ["gpt-5.4", "gpt-5.4-pro", "gpt-5.4-mini", "gpt-5.4-nano"];
const MINI_MODELS = new Set(["gpt-5.4-mini", "gpt-5.4-nano"]);
const EXCLUDED_CONFIG_PATHS = new Set([
  "llm",
  "mode.nl_only_mode",
  "mode.lean_mode",
  "mode.lean.enabled",
  "mode.lean.use_v2_endpoints",
  "mode.lean.use_v2_prepare_track",
  "mode.lean.fallback_to_v1_on_error",
  "mode.lean.stream_progress_payloads",
  "lean_engine.repair_context_token_budget",
]);
const CONFIG_FIELD_HELP = {
  "decomposition.parallel_root_decompositions_n":
    "How many root decomposition candidates can run in parallel.",
  "decomposition.parallel_root_take_k":
    "How many vetted root decomposition candidates to keep (active + standby).",
  "decomposition.root_solutions_required_for_termination":
    "How many independent root tracks must finish before the problem is marked succeeded.",
  "decomposition.lemma_decomposition_candidates_n":
    "How many Agent2 lemma decomposition candidates to request per failed-lemma decomposition round.",
  "decomposition.max_decompositions_per_failed_lemma":
    "Maximum decomposition attempts allowed for a failed lemma before the whole run fails.",
  "decomposition.max_consecutive_fatal_rejections_per_node":
    "How many consecutive fatal decomposition rejections are allowed on the same node before failing it.",
  "lemma_solving.max_consecutive_fatal_rejections_per_lemma":
    "How many consecutive fatal rejections a lemma can accumulate before decomposition is attempted.",
  "lemma_solving.max_minor_rejections_per_lemma":
    "Cumulative number of minor rejections a lemma can accumulate before decomposition is attempted.",
  "lemma_solving.max_total_lemma_nodes":
    "Global cap on total lemma nodes created for this problem.",
  "lean_engine.model":
    "Optional Lean-engine model override used for assembly checks, lemma formalization, plausibility checks, and root assembly.",
  "lean_engine.no_lean4_refs":
    "If true, skip injecting the large lean4-skills reference library into Lean prompts. This makes prompts much smaller and is useful when you want shorter first-turn context.",
  "lean_engine.max_workers":
    "How many Lean jobs this problem is allowed to run in parallel. This is the single concurrency knob for Lean work.",
  "lean_engine.lean_job_timeout_seconds":
    "Shared timeout for normal Lean jobs like prepare-track, formalize-lemma, and plausibility checks. Use 0 for no limit.",
  "lean_engine.assemble_root_timeout_seconds":
    "Timeout for the final assemble_root job once a winning decomposition is selected. Use 0 for no limit.",
  "lean_engine.claude_activity_timeout_seconds":
    "Shared Claude idle/tool-wait timeout inside Lean jobs. Use 0 for no limit.",
  "lean_engine.claude_init_timeout_seconds":
    "Claude first-token/startup timeout inside Lean jobs. Use 0 for no limit.",
  "final_check.fail_problem_on_fatal":
    "If true, any fatal Agent 6 finding fails the whole problem instead of reopening only the cited lemmas.",
};

function byId(id) {
  return document.getElementById(id);
}

function encodeArtifactPath(key) {
  return key
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
}

async function api(path, options = {}) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const response = await fetch(path, options);
    const body = await response.json().catch(async () => ({ raw: await response.text() }));
    if (response.status === 503 && body?.error?.code === "db_locked" && attempt < 3) {
      await new Promise((r) => setTimeout(r, 500 + attempt * 500));
      continue;
    }
    if (!response.ok) {
      const msg = body?.error?.message || body?.raw || `HTTP ${response.status}`;
      const err = new Error(msg);
      err.status = response.status;
      err.body = body;
      throw err;
    }
    return body;
  }
}

function setMessage(message) {
  byId("message-line").textContent = message;
}

function setBadge(id, text) {
  byId(id).textContent = text;
}

function setAutoRunBadge(text) {
  setBadge("badge-auto", `auto-run: ${text}`);
}

function setRefreshBadge() {
  const intervalMs = document.hidden ? 8000 : 2000;
  setBadge("badge-refresh", `refresh: live (${Math.round(intervalMs / 1000)}s)`);
}

function problemIdFromUrl() {
  try {
    const url = new URL(window.location.href);
    return (url.searchParams.get("problem_id") || "").trim() || null;
  } catch {
    return null;
  }
}

function pageFromUrl() {
  try {
    const url = new URL(window.location.href);
    return url.searchParams.get("page") === "submit" ? "submit" : "problem";
  } catch {
    return "problem";
  }
}

function setProblemInUrl(problemId) {
  try {
    const url = new URL(window.location.href);
    if (problemId) {
      url.searchParams.set("problem_id", problemId);
    } else {
      url.searchParams.delete("problem_id");
    }
    window.history.replaceState({}, "", url.toString());
  } catch {
    // no-op
  }
}

function setPageInUrl(pageName) {
  try {
    const url = new URL(window.location.href);
    if (pageName === "submit") {
      url.searchParams.set("page", "submit");
    } else {
      url.searchParams.delete("page");
    }
    window.history.replaceState({}, "", url.toString());
  } catch {
    // no-op
  }
}

function renderSubmitPageContext() {
  const context = byId("submit-page-context");
  if (!context) {
    return;
  }
  if (state.currentProblemId) {
    context.textContent = `Current problem: ${state.currentProblemId}. Submit a new problem here, or use the selector above to jump back into monitoring.`;
    return;
  }
  context.textContent = "Create and launch a fresh problem here. Use the problem selector above to switch straight into monitoring any existing run.";
}

function activatePage(pageName) {
  const normalized = pageName === "submit" ? "submit" : "problem";
  state.currentPage = normalized;
  document.querySelectorAll(".page-view").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `page-${normalized}`);
  });
  setPageInUrl(normalized);
  renderSubmitPageContext();
}

function updateContinueButtonState() {
  const button = byId("continue-problem");
  if (!button) {
    return;
  }
  const status = state.snapshot?.problem?.status || null;
  const canContinue = Boolean(state.currentProblemId && status && status !== "succeeded");
  button.disabled = !canContinue || state.controlInFlight;
}

function updatePauseButtonState() {
  const button = byId("pause-problem");
  if (!button) {
    return;
  }
  const status = state.snapshot?.problem?.status || null;
  const canPause = Boolean(
    state.currentProblemId
    && status
    && status !== "succeeded"
    && status !== "failed"
    && status !== "paused",
  );
  button.disabled = !canPause || state.controlInFlight;
}

function updateOpenProblemButtonState() {
  const button = byId("open-problem");
  const select = byId("problem-select");
  if (!button || !select) {
    return;
  }
  button.disabled = !(select.value || "").trim();
}

function syncAutoRunWithProblemStatus() {
  if (!state.currentProblemId) {
    stopAutoRun();
    return;
  }
  const status = state.snapshot?.problem?.status || null;
  if (status === "paused") {
    state.pauseHold = true;
  }
  if (state.pauseHold) {
    stopAutoRun();
    return;
  }
  if (status === "failed" || status === "succeeded" || status === "paused") {
    stopAutoRun();
    return;
  }
  stopAutoRun();
}

function fmtInt(value) {
  const n = Number.isFinite(Number(value)) ? Number(value) : 0;
  return Math.round(n).toLocaleString();
}

function fmtUsd(value) {
  const n = Number.isFinite(Number(value)) ? Number(value) : 0;
  return `$${n.toFixed(6)}`;
}

function fmtDurationSeconds(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return "-";
  }
  if (n < 60) {
    return `${n.toFixed(n >= 10 ? 1 : 2)}s`;
  }
  const hours = Math.floor(n / 3600);
  const minutes = Math.floor((n % 3600) / 60);
  const seconds = Math.round(n % 60);
  if (hours > 0) {
    return `${hours}h ${String(minutes).padStart(2, "0")}m ${String(seconds).padStart(2, "0")}s`;
  }
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

function safeJsonParse(text, fallback = null) {
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

function summarizeLeanResult(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    return null;
  }
  const summary = {};
  if (typeof result.status === "string" && result.status) {
    summary.status = result.status;
  }
  if (typeof result.operation === "string" && result.operation) {
    summary.operation = result.operation;
  }
  if (typeof result.error_class === "string" && result.error_class) {
    summary.error_class = result.error_class;
  }
  if (typeof result.error_scope === "string" && result.error_scope) {
    summary.error_scope = result.error_scope;
  }
  if (typeof result.recommended_next_step === "string" && result.recommended_next_step) {
    summary.recommended_next_step = result.recommended_next_step;
  }
  if (typeof result.routing_confidence === "number" && Number.isFinite(result.routing_confidence)) {
    summary.routing_confidence = Math.round(result.routing_confidence * 1000) / 1000;
  }
  return Object.keys(summary).length ? summary : null;
}

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function copyTextToClipboard(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "true");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.focus();
      area.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(area);
      if (!ok) {
        reject(new Error("copy command failed"));
        return;
      }
      resolve();
    } catch (error) {
      reject(error);
    }
  });
}

function installCopyButtons() {
  const jsonPreIds = [
    "submit-response",
    "overview-running-final-proof",
    "overview-final-nl-output",
    "tree-node-detail",
    "request-log-detail",
    "artifact-content",
    "raw-snapshot",
    "raw-progress",
    "overview-problem-input-json",
  ];
  jsonPreIds.forEach((id) => {
    const pre = byId(id);
    if (!pre || pre.dataset.copyInstalled === "true") {
      return;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "copy-pre-wrap";
    pre.parentNode.insertBefore(wrapper, pre);
    wrapper.appendChild(pre);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy-pre-btn";
    button.setAttribute("aria-label", "Copy JSON");
    button.setAttribute("title", "Copy");
    button.innerHTML = '<span class="copy-icon" aria-hidden="true"></span>';
    button.addEventListener("click", async () => {
      const text = pre.textContent || "";
      try {
        await copyTextToClipboard(text);
        button.classList.add("copied");
        setTimeout(() => {
          button.classList.remove("copied");
        }, 1200);
      } catch (error) {
        setMessage(`Copy error: ${error?.message || "failed to copy"}`);
      }
    });
    wrapper.appendChild(button);
    pre.dataset.copyInstalled = "true";
  });
}

function normalizeModelSelection(model) {
  const raw = String(model || "").trim().toLowerCase();
  return MODEL_OPTIONS.includes(raw) ? raw : "gpt-5.4-nano";
}

function enforceMiniReasoningConstraint(agentKey) {
  const modelSelect = byId(`form-${agentKey}-model`);
  const thinkingSelect = byId(`form-${agentKey}-thinking`);
  if (!modelSelect || !thinkingSelect) {
    return;
  }
  const xhighOption = [...thinkingSelect.options].find((opt) => opt.value === "xhigh");
  const isMini = MINI_MODELS.has(modelSelect.value);
  if (xhighOption) {
    xhighOption.disabled = isMini;
  }
  if (isMini && thinkingSelect.value === "xhigh") {
    thinkingSelect.value = "high";
  }
}

function toggleFirstAttemptRow(key, enabled) {
  ["model", "thinking", "verbosity", "timeout"].forEach((field) => {
    const el = byId(`form-${key}-${field}`);
    if (el) {
      el.disabled = !enabled;
    }
  });
  if (enabled) {
    enforceMiniReasoningConstraint(key);
  }
}

function installFirstAttemptToggleListeners() {
  FIRST_ATTEMPT_AGENT_SPECS.forEach(({ key }) => {
    const checkbox = byId(`form-${key}-enable`);
    if (!checkbox) return;
    checkbox.addEventListener("change", () => {
      toggleFirstAttemptRow(key, checkbox.checked);
    });
    const modelSelect = byId(`form-${key}-model`);
    if (modelSelect) {
      modelSelect.addEventListener("change", () => enforceMiniReasoningConstraint(key));
    }
  });
}

function applyAgentLlmRowToForm(agentKey, row) {
  const modelValue = normalizeModelSelection(row.model);
  byId(`form-${agentKey}-model`).value = modelValue;
  const thinking =
    typeof row.thinking_level === "string"
      ? row.thinking_level
      : typeof row.reasoning_effort === "string"
      ? row.reasoning_effort
      : "none";
  byId(`form-${agentKey}-thinking`).value = thinking;
  const verbosity =
    typeof row.verbosity === "string"
      ? row.verbosity
      : typeof row.text_verbosity === "string"
      ? row.text_verbosity
      : typeof row.text?.verbosity === "string"
      ? row.text.verbosity
      : "medium";
  byId(`form-${agentKey}-verbosity`).value = verbosity;
  const timeoutSeconds =
    typeof row.timeout_seconds === "number" && Number.isFinite(row.timeout_seconds) && row.timeout_seconds >= 0
      ? String(Math.floor(row.timeout_seconds))
      : "";
  byId(`form-${agentKey}-timeout`).value = timeoutSeconds;
  enforceMiniReasoningConstraint(agentKey);
}

function applyLlmOverridesToForm(llmConfig) {
  AGENT_KEYS.forEach((agentKey) => {
    applyAgentLlmRowToForm(agentKey, llmConfig?.[agentKey] || {});
  });
  FIRST_ATTEMPT_AGENT_SPECS.forEach(({ key, llmKey }) => {
    const row = llmConfig?.[llmKey];
    const checkbox = byId(`form-${key}-enable`);
    if (!checkbox) return;
    const isEnabled = row !== null && row !== undefined;
    checkbox.checked = isEnabled;
    toggleFirstAttemptRow(key, isEnabled);
    if (isEnabled) {
      applyAgentLlmRowToForm(key, row);
    }
  });
}

function agentLlmRowFromForm(agentKey) {
  const model = normalizeModelSelection(byId(`form-${agentKey}-model`).value);
  const thinking = byId(`form-${agentKey}-thinking`).value.trim();
  const verbosity = byId(`form-${agentKey}-verbosity`).value.trim();
  const timeoutRaw = byId(`form-${agentKey}-timeout`).value.trim();
  const timeout = timeoutRaw === "" ? null : Number.parseInt(timeoutRaw, 10);
  if (timeoutRaw !== "" && (!Number.isFinite(timeout) || timeout < 0)) {
    throw new Error(`${agentKey} timeout must be an integer >= 0`);
  }
  if (MINI_MODELS.has(model) && thinking === "xhigh") {
    throw new Error(`${agentKey} cannot use xhigh reasoning with ${model}`);
  }
  const entry = {
    model,
    thinking_level: thinking || "none",
    verbosity: verbosity || "medium",
  };
  if (timeout !== null) {
    entry.timeout_seconds = timeout;
  }
  return entry;
}

function llmOverridesFromForm() {
  const llm = {};
  AGENT_KEYS.forEach((agentKey) => {
    llm[agentKey] = agentLlmRowFromForm(agentKey);
  });
  FIRST_ATTEMPT_AGENT_SPECS.forEach(({ key, llmKey }) => {
    const checkbox = byId(`form-${key}-enable`);
    if (!checkbox) return;
    if (checkbox.checked) {
      llm[llmKey] = agentLlmRowFromForm(key);
    }
  });
  return llm;
}

function getNestedValue(obj, path) {
  let current = obj;
  for (const key of path) {
    if (!current || typeof current !== "object" || !(key in current)) {
      return undefined;
    }
    current = current[key];
  }
  return current;
}

function setNestedValue(obj, path, value) {
  let current = obj;
  for (let index = 0; index < path.length - 1; index += 1) {
    const key = path[index];
    if (!current[key] || typeof current[key] !== "object" || Array.isArray(current[key])) {
      current[key] = {};
    }
    current = current[key];
  }
  current[path[path.length - 1]] = value;
}

function humanizeIdentifier(value) {
  return String(value || "")
    .split("_")
    .filter(Boolean)
    .map((part) => {
      const lower = part.toLowerCase();
      if (lower === "nl") {
        return "NL";
      }
      if (lower === "llm") {
        return "LLM";
      }
      if (/^\d+$/.test(part)) {
        return part;
      }
      if (part.length <= 2) {
        return part.toUpperCase();
      }
      return part.charAt(0).toUpperCase() + part.slice(1);
    })
    .join(" ");
}

function defaultConfigFieldDescription(spec) {
  const phrase = spec.label.replace(/_/g, " ");
  if (spec.label.startsWith("max_")) {
    const capped = spec.label.slice(4).replace(/_/g, " ");
    return `Maximum allowed ${capped}.`;
  }
  if (spec.label.startsWith("parallel_")) {
    return `Controls ${phrase} in parallel execution.`;
  }
  return `Controls ${phrase}.`;
}

function configFieldDescription(spec) {
  return CONFIG_FIELD_HELP[spec.fullPath] || defaultConfigFieldDescription(spec);
}

function buildConfigFieldSpecs(configTemplate) {
  const specs = [];
  const walk = (value, path, section) => {
    const fullPath = path.join(".");
    if (EXCLUDED_CONFIG_PATHS.has(fullPath)) {
      return;
    }
    if (Array.isArray(value)) {
      specs.push({
        path,
        fullPath,
        section,
        label: path[path.length - 1],
        type: "json",
        defaultValue: JSON.stringify(value, null, 2),
      });
      return;
    }
    if (value === null) {
      specs.push({
        path,
        fullPath,
        section,
        label: path[path.length - 1],
        type: "string",
        defaultValue: "",
      });
      return;
    }
    if (typeof value === "object") {
      Object.entries(value).forEach(([key, nested]) => walk(nested, [...path, key], section));
      return;
    }
    if (typeof value === "boolean") {
      specs.push({
        path,
        fullPath,
        section,
        label: path[path.length - 1],
        type: "boolean",
        defaultValue: value,
      });
      return;
    }
    if (typeof value === "number") {
      specs.push({
        path,
        fullPath,
        section,
        label: path[path.length - 1],
        type: "number",
        defaultValue: value,
      });
      return;
    }
    specs.push({
      path,
      fullPath,
      section,
      label: path[path.length - 1],
      type: "string",
      defaultValue: String(value),
    });
  };

  const source = configTemplate && typeof configTemplate === "object" ? configTemplate : {};
  Object.entries(source).forEach(([section, value]) => {
    if (EXCLUDED_CONFIG_PATHS.has(section)) {
      return;
    }
    walk(value, [section], section);
  });
  return specs.map((spec) => ({
    ...spec,
    displayLabel: humanizeIdentifier(spec.label),
    relativePath: spec.path.slice(1).join(".") || spec.label,
    description: configFieldDescription(spec),
  }));
}

function renderConfigFields(configTemplate) {
  state.configFieldSpecs = buildConfigFieldSpecs(configTemplate);
  const host = byId("form-config-fields");
  host.innerHTML = "";
  const bySection = {};
  state.configFieldSpecs.forEach((spec) => {
    bySection[spec.section] = bySection[spec.section] || [];
    bySection[spec.section].push(spec);
  });

  Object.entries(bySection)
    .sort(([a], [b]) => a.localeCompare(b))
    .forEach(([section, specs]) => {
      const sectionDiv = document.createElement("div");
      sectionDiv.className = "config-section";
      const title = document.createElement("h4");
      title.textContent = humanizeIdentifier(section);
      sectionDiv.appendChild(title);

      const grid = document.createElement("div");
      grid.className = "config-section-grid";
      specs
        .sort((a, b) => a.fullPath.localeCompare(b.fullPath))
        .forEach((spec) => {
          const wrapper = document.createElement("label");
          wrapper.className = "config-field";
          const name = document.createElement("span");
          name.className = "config-name";
          name.textContent = spec.displayLabel;
          wrapper.appendChild(name);
          const path = document.createElement("span");
          path.className = "config-path";
          path.textContent = spec.relativePath;
          wrapper.appendChild(path);

          const inputId = `form-config-${spec.fullPath.replace(/\./g, "-")}`;
          spec.inputId = inputId;
          if (spec.type === "boolean") {
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.id = inputId;
            checkbox.checked = Boolean(spec.defaultValue);
            wrapper.appendChild(checkbox);
          } else if (spec.type === "number") {
            const number = document.createElement("input");
            number.type = "number";
            number.id = inputId;
            number.step = "any";
            number.value = String(spec.defaultValue);
            wrapper.appendChild(number);
          } else if (spec.type === "json") {
            const area = document.createElement("textarea");
            area.id = inputId;
            area.rows = 4;
            area.value = spec.defaultValue;
            wrapper.appendChild(area);
          } else {
            const text = document.createElement("input");
            text.type = "text";
            text.id = inputId;
            text.value = String(spec.defaultValue ?? "");
            wrapper.appendChild(text);
          }
          const help = document.createElement("span");
          help.className = "config-help";
          help.textContent = spec.description;
          wrapper.appendChild(help);
          grid.appendChild(wrapper);
        });
      sectionDiv.appendChild(grid);
      host.appendChild(sectionDiv);
    });
}

function applyConfigToFields(config) {
  const source = config && typeof config === "object" ? config : {};
  state.configFieldSpecs.forEach((spec) => {
    const input = byId(spec.inputId);
    if (!input) {
      return;
    }
    const value = getNestedValue(source, spec.path);
    if (spec.type === "boolean") {
      input.checked = typeof value === "boolean" ? value : Boolean(spec.defaultValue);
      return;
    }
    if (spec.type === "number") {
      const nextValue =
        typeof value === "number" && Number.isFinite(value) ? value : Number(spec.defaultValue);
      input.value = String(nextValue);
      return;
    }
    if (spec.type === "json") {
      if (value === undefined) {
        input.value = spec.defaultValue;
      } else {
        input.value = JSON.stringify(value, null, 2);
      }
      return;
    }
    input.value = value === undefined || value === null ? String(spec.defaultValue ?? "") : String(value);
  });
}

function configFromFields() {
  const config = {};
  state.configFieldSpecs.forEach((spec) => {
    const input = byId(spec.inputId);
    if (!input) {
      return;
    }
    let value;
    if (spec.type === "boolean") {
      value = Boolean(input.checked);
    } else if (spec.type === "number") {
      const raw = String(input.value).trim();
      const num = Number(raw);
      if (raw === "" || !Number.isFinite(num)) {
        throw new Error(`config.${spec.fullPath} must be a valid number`);
      }
      value = Number.isInteger(spec.defaultValue) ? Number.parseInt(raw, 10) : num;
    } else if (spec.type === "json") {
      const raw = String(input.value).trim();
      const parsed = safeJsonParse(raw, null);
      if (parsed === null) {
        throw new Error(`config.${spec.fullPath} must be valid JSON`);
      }
      value = parsed;
    } else {
      value = String(input.value);
    }
    setNestedValue(config, spec.path, value);
  });
  return config;
}

function activateTab(tabName) {
  document.querySelectorAll("#tabs-nav .tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabName);
  });
  document.querySelectorAll(".tab-content").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${tabName}`);
  });
}

function renderProblemBadges() {
  const problem = state.snapshot?.problem;
  if (!problem) {
    setBadge("badge-problem", "problem: none");
    setBadge("badge-status", "status: -");
    setBadge("badge-verification", "verification: -");
    setBadge("badge-mode", "mode: -");
    return;
  }
  setBadge("badge-problem", `problem: ${problem.problem_id}`);
  setBadge("badge-status", `status: ${problem.status}`);
  setBadge("badge-verification", `verification: ${problem.verification_level}`);
  const leanMode = typeof problem.lean_mode === "boolean" ? problem.lean_mode : !problem.nl_only_mode;
  setBadge("badge-mode", `mode: ${leanMode ? "lean" : "nl_only"}`);
}

function initialPayloadFromTemplate() {
  const defaultPayload = {
    title: "Putnam",
    statement_nl: "",
    statement_lean: null,
    imports: ["Mathlib"],
    lean_image_tag: "Orthosolver-lean-4.18.0-mathlib-v4.18.0",
    initial_trusted_context: [],
    config: {
      decomposition: {
        parallel_root_decompositions_n: 2,
        parallel_root_take_k: 1,
        root_solutions_required_for_termination: 1,
        lemma_decomposition_candidates_n: 1,
        max_decompositions_per_failed_lemma: 10,
        max_consecutive_fatal_rejections_per_node: 5,
      },
      lemma_solving: {
        max_consecutive_fatal_rejections_per_lemma: 3,
        max_minor_rejections_per_lemma: 10,
        max_total_lemma_nodes: 5000,
      },
      mode: { nl_only_mode: true },
      llm: {
        agent1: { model: "gpt-5.4-nano", thinking_level: "medium", verbosity: "medium", timeout_seconds: 600 },
        agent2: { model: "gpt-5.4", thinking_level: "xhigh", verbosity: "medium", timeout_seconds: 600 },
        agent3: { model: "gpt-5.4", thinking_level: "high", verbosity: "medium", timeout_seconds: 600 },
        agent4: { model: "gpt-5.4", thinking_level: "xhigh", verbosity: "medium", timeout_seconds: 600 },
        agent5: { model: "gpt-5.4", thinking_level: "high", verbosity: "medium", timeout_seconds: 600 },
        agent6: { model: "gpt-5.4", thinking_level: "xhigh", verbosity: "medium", timeout_seconds: 600 },
        agent7: { model: "gpt-5.4", thinking_level: "xhigh", verbosity: "medium", timeout_seconds: 600 },
        agent8: { model: "gpt-5.4", thinking_level: "high", verbosity: "medium", timeout_seconds: 600 },
      },
    },
  };
  if (!state.template) {
    return defaultPayload;
  }
  const payload = deepClone(state.template);
  payload.title = payload.title || defaultPayload.title;
  payload.statement_nl = payload.statement_nl ?? defaultPayload.statement_nl;
  payload.config = payload.config || defaultPayload.config;
  payload.config.mode = payload.config.mode || defaultPayload.config.mode;
  if (typeof payload.config.mode.nl_only_mode !== "boolean") {
    payload.config.mode.nl_only_mode = true;
  }
  delete payload.config.mode.lean_mode;
  return payload;
}

function applyPayloadToForm(payload) {
  byId("form-title").value = payload.title || "";
  byId("form-statement-nl").value = payload.statement_nl || "";
  byId("form-statement-lean").value = payload.statement_lean || "";
  byId("form-imports").value = (payload.imports || []).join(", ");
  byId("form-lean-image-tag").value = payload.lean_image_tag || "";
  byId("form-nl-only").checked = Boolean(payload.config?.mode?.nl_only_mode);
  byId("form-initial-trusted-context").value = JSON.stringify(payload.initial_trusted_context || [], null, 2);

  const config = deepClone(payload.config || {});
  if (!state.configFieldSpecs.length) {
    renderConfigFields(deepClone(state.template?.config || config));
  }
  applyLlmOverridesToForm(config.llm || {});
  delete config.llm;
  applyConfigToFields(config);
}

function payloadFromForm() {
  const base = initialPayloadFromTemplate();
  base.title = byId("form-title").value.trim();
  base.statement_nl = byId("form-statement-nl").value.trim();
  const statementLean = byId("form-statement-lean").value.trim();
  base.statement_lean = statementLean || null;
  base.imports = byId("form-imports")
    .value.split(",")
    .map((x) => x.trim())
    .filter((x) => x.length > 0);
  if (base.imports.length === 0) {
    base.imports = ["Mathlib"];
  }
  base.lean_image_tag = byId("form-lean-image-tag").value.trim() || base.lean_image_tag;

  const trusted = safeJsonParse(byId("form-initial-trusted-context").value, null);
  if (!Array.isArray(trusted)) {
    throw new Error("Initial trusted context must be a JSON array");
  }
  base.initial_trusted_context = trusted;

  base.config = configFromFields();
  const nlOnlyMode = Boolean(byId("form-nl-only").checked);
  base.config.mode = {
    ...(base.config.mode || {}),
    nl_only_mode: nlOnlyMode,
  };

  const llmOverrides = llmOverridesFromForm();
  if (Object.keys(llmOverrides).length > 0) {
    base.config.llm = llmOverrides;
  } else if (base.config.llm) {
    delete base.config.llm;
  }

  return base;
}

function syncFormToJsonEditor() {
  const payload = payloadFromForm();
  byId("request-json").value = JSON.stringify(payload, null, 2);
}

function syncJsonToForm() {
  const parsed = safeJsonParse(byId("request-json").value, null);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Raw request JSON is not a valid object");
  }
  applyPayloadToForm(parsed);
}

async function loadTemplate() {
  const payload = await api("/v1/debug/problem-create-template");
  state.template = payload.template;
  renderConfigFields(deepClone(state.template.config || {}));
  const initial = initialPayloadFromTemplate();
  applyPayloadToForm(initial);
  byId("request-json").value = JSON.stringify(initial, null, 2);
}

async function submitRequestFromEditor() {
  const raw = byId("request-json").value;
  const payload = safeJsonParse(raw, null);
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Raw request JSON is invalid");
  }

  const response = await api("/v1/problems", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  await api(`/v1/problems/${response.problem_id}/start`, {
    method: "POST",
    headers: { "X-Debug-Run-Trigger": "submit_auto" },
  });
  byId("submit-response").textContent = JSON.stringify(response, null, 2);
  setMessage(`Created problem ${response.problem_id}`);
  await refreshProblemList();
  await loadProblem(response.problem_id, "problem");
  return response;
}

async function runTickForProblem(problemId, trigger) {
  return api(`/v1/problems/${problemId}/start`, {
    method: "POST",
    headers: { "X-Debug-Run-Trigger": trigger },
  });
}

async function refreshProblemList() {
  const payload = await api("/v1/debug/problems?limit=200");
  const select = byId("problem-select");
  const previous = state.currentProblemId;
  const fromUrl = problemIdFromUrl();
  const preferred = previous || fromUrl;
  select.innerHTML = "";
  let selectedValue = null;
  payload.problems.forEach((row) => {
    const opt = document.createElement("option");
    opt.value = row.problem_id;
    opt.textContent = `${row.problem_id} [${row.status}] ${row.title}`;
    if (row.problem_id === preferred) {
      opt.selected = true;
      selectedValue = row.problem_id;
    }
    select.appendChild(opt);
  });
  if (!selectedValue && payload.problems.length > 0) {
    selectedValue = payload.problems[0].problem_id;
    select.value = selectedValue;
  }
  if (selectedValue) {
    setProblemInUrl(selectedValue);
  } else {
    setProblemInUrl(null);
  }
  updateOpenProblemButtonState();
}

function closeEventStream() {
  if (state.sse) {
    state.sse.close();
    state.sse = null;
  }
}

async function reconcileIncompleteRuns() {
  if (!state.currentProblemId) {
    return;
  }
  await api(`/v1/debug/problems/${state.currentProblemId}/reconcile-incomplete-runs`, { method: "POST" });
}

function stopLiveRefresh() {
  if (state.liveRefreshTimer) {
    clearTimeout(state.liveRefreshTimer);
    state.liveRefreshTimer = null;
  }
  setRefreshBadge();
}

function startLiveRefresh() {
  if (!state.currentProblemId) {
    stopLiveRefresh();
    return;
  }
  if (state.liveRefreshTimer) {
    setRefreshBadge();
    return;
  }

  const tick = async () => {
    if (!state.currentProblemId) {
      stopLiveRefresh();
      return;
    }
    try {
      const now = Date.now();
      if (now >= state.nextReconcileAtMs) {
        await reconcileIncompleteRuns();
        state.nextReconcileAtMs = now + 30000;
      }
      await refreshCurrentProblem();
    } catch (error) {
      setMessage(`Live refresh error: ${error.message}`);
    } finally {
      if (state.currentProblemId) {
        const delay = document.hidden ? 8000 : 2000;
        state.liveRefreshTimer = setTimeout(tick, delay);
      } else {
        state.liveRefreshTimer = null;
      }
      setRefreshBadge();
    }
  };

  const initialDelay = document.hidden ? 8000 : 2000;
  state.liveRefreshTimer = setTimeout(tick, initialDelay);
  setRefreshBadge();
}

function scheduleRefresh() {
  if (!state.currentProblemId || state.refreshScheduled) {
    return;
  }
  state.refreshScheduled = true;
  setTimeout(async () => {
    state.refreshScheduled = false;
    try {
      await refreshCurrentProblem();
    } catch (error) {
      setMessage(`Auto refresh error: ${error.message}`);
    }
  }, 350);
}

function openEventStream(problemId) {
  closeEventStream();
  state.sse = new EventSource(`/v1/problems/${problemId}/events/stream`);
  state.sse.addEventListener("event", scheduleRefresh);
  state.sse.addEventListener("terminal", () => {
    scheduleRefresh();
  });
  state.sse.addEventListener("heartbeat", () => {});
}

async function fetchSnapshot() {
  if (!state.currentProblemId) {
    state.snapshot = null;
    return;
  }
  state.snapshot = await api(`/v1/debug/problems/${state.currentProblemId}/snapshot?events_limit=${state.eventsLimit}`);
}

async function fetchProgress() {
  if (!state.currentProblemId) {
    state.progress = null;
    return;
  }
  state.progress = await api(`/v1/problems/${state.currentProblemId}/progress`);
}

async function fetchCostSummary() {
  if (!state.currentProblemId) {
    state.costSummary = null;
    return;
  }
  state.costSummary = await api(`/v1/problems/${state.currentProblemId}/cost`);
}

async function fetchLlmUsageSummary() {
  if (!state.currentProblemId) {
    state.llmUsage = null;
    return;
  }
  state.llmUsage = await api(`/v1/debug/problems/${state.currentProblemId}/llm-usage`);
}

async function fetchProblemInput() {
  if (!state.currentProblemId) {
    state.problemInput = null;
    return;
  }
  try {
    state.problemInput = await api(`/v1/debug/problems/${state.currentProblemId}/input-json`);
  } catch (error) {
    const code = error?.body?.error?.code;
    if (error?.status === 404 && code === "problem_input_not_found") {
      state.problemInput = null;
      return;
    }
    throw error;
  }
}

async function fetchArtifacts() {
  if (!state.currentProblemId) {
    state.artifacts = [];
    return;
  }
  const query = new URLSearchParams({
    include_worker_jobs: byId("artifact-include-worker").checked ? "true" : "false",
    limit: "1200",
  });
  const prefix = byId("artifact-prefix").value.trim();
  if (prefix) {
    query.set("prefix", prefix);
  }
  const payload = await api(`/v1/debug/problems/${state.currentProblemId}/artifacts?${query.toString()}`);
  state.artifacts = payload.artifacts || [];
}

async function fetchRequestLog() {
  if (!state.currentProblemId) {
    state.requestLog = [];
    return;
  }
  const sourceFilter = byId("request-source-filter").value;
  const params = new URLSearchParams({ limit: "800" });
  if (sourceFilter) {
    params.set("source", sourceFilter);
  }
  const payload = await api(`/v1/debug/problems/${state.currentProblemId}/request-log?${params.toString()}`);
  const rows = Array.isArray(payload.entries) ? payload.entries : [];
  state.requestLog = sourceFilter ? rows : rows.filter((entry) => !REQUEST_CONSOLE_HIDDEN_SOURCES.has(entry.source));
}

async function fetchRequestArtifacts() {
  if (!state.currentProblemId) {
    state.requestArtifacts = [];
    return;
  }
  const params = new URLSearchParams({
    prefix: `problems/${state.currentProblemId}`,
    include_worker_jobs: "false",
    limit: "5000",
  });
  const payload = await api(`/v1/debug/problems/${state.currentProblemId}/artifacts?${params.toString()}`);
  state.requestArtifacts = payload.artifacts || [];
}

function renderOverview() {
  const cards = byId("overview-cards");
  const decList = byId("overview-decompositions");
  const lemList = byId("overview-lemmas");
  const usageList = byId("overview-usage-stages");
  const runningFinalProof = byId("overview-running-final-proof");
  const finalOutput = byId("overview-final-nl-output");
  const problemInputMeta = byId("overview-problem-input-meta");
  const problemInput = byId("overview-problem-input-json");

  cards.innerHTML = "";
  decList.innerHTML = "";
  lemList.innerHTML = "";
  usageList.innerHTML = "";

  if (!state.snapshot) {
    cards.innerHTML = '<div class="card"><div class="key">State</div><div>No problem selected</div></div>';
    runningFinalProof.textContent = "Running final proof is not available yet.";
    finalOutput.textContent = "NL-only final output is not available yet.";
    problemInputMeta.textContent = "Select a problem to view its original create payload.";
    problemInput.textContent = "No problem selected.";
    updateContinueButtonState();
    return;
  }

  const p = state.snapshot.problem;
  updateContinueButtonState();
  const latestEvent = state.snapshot.events[state.snapshot.events.length - 1];
  const hasNonCreateEvents = (state.snapshot.events || []).some((row) => row.stage !== "problem.created");
  if (p.status === "created" && !hasNonCreateEvents) {
    const hint = document.createElement("div");
    hint.className = "card info";
    hint.innerHTML =
      "<div class=\"key\">Lifecycle Hint</div><div>Created only. Execution has not been started yet.</div>";
    cards.appendChild(hint);
  }

  const items = [
    ["problem_id", p.problem_id],
    ["status", p.status],
    ["verification", p.verification_level],
    ["nl_only_mode", String(p.nl_only_mode)],
    ["lean_mode", String(typeof p.lean_mode === "boolean" ? p.lean_mode : !p.nl_only_mode)],
    ["active_decomposition", p.active_decomposition_id || "-"],
    ["standby_decomposition", p.standby_decomposition_id || "-"],
    ["lemma_count", (state.snapshot.visible_lemma_ids || []).length],
    ["decomposition_count", state.snapshot.decompositions.length],
    ["ready_for_lean_count", p.ready_for_lean_count ?? 0],
    ["latest_stage", latestEvent ? latestEvent.stage : "-"],
  ];
  if (p.lean_session) {
    items.push(["lean_session_status", p.lean_session.status || "-"]);
    items.push(["lean_session_max_workers", p.lean_session.max_workers ?? "-"]);
    items.push(["lean_session_active_jobs", p.lean_session.active_jobs ?? "-"]);
    items.push(["lean_session_queue_depth", p.lean_session.queue_depth ?? "-"]);
  }

  const usageTotals = state.llmUsage?.totals || null;
  const pricingByModel = state.llmUsage?.pricing_by_model_usd_per_1m || null;
  if (usageTotals) {
    items.push(["llm_calls", fmtInt(usageTotals.call_count || 0)]);
    items.push(["input_tokens", fmtInt(usageTotals.input_tokens || 0)]);
    items.push(["cached_input_tokens", fmtInt(usageTotals.cached_input_tokens || 0)]);
    items.push(["output_tokens", fmtInt(usageTotals.output_tokens || 0)]);
    items.push(["total_tokens", fmtInt(usageTotals.total_tokens || 0)]);
    items.push(["estimated_cost_usd", fmtUsd(usageTotals.estimated_cost_usd || 0)]);
  } else if (state.costSummary) {
    items.push(["input_tokens", fmtInt(state.costSummary.total_input_tokens || 0)]);
    items.push(["output_tokens", fmtInt(state.costSummary.total_output_tokens || 0)]);
    items.push(["estimated_cost_usd", fmtUsd(state.costSummary.total_estimated_cost_usd || 0)]);
  }
  if (pricingByModel) {
    Object.entries(pricingByModel).forEach(([model, pricing]) => {
      items.push([
        `${model}_pricing_per_1m`,
        `in ${fmtUsd(pricing.input || 0)}, cached ${fmtUsd(pricing.cached_input || 0)}, out ${fmtUsd(pricing.output || 0)}`,
      ]);
    });
  }
  items.forEach(([key, value]) => {
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `<div class="key">${key}</div><div>${value}</div>`;
    cards.appendChild(div);
  });

  const logicalDecompositions = state.snapshot.logical_decompositions || state.snapshot.decompositions || [];
  const autoSplitEnabled = !!state.snapshot.problem?.config?.mode?.lean?.auto_split_sublemmas;
  logicalDecompositions.forEach((row) => {
    const div = document.createElement("div");
    div.className = "list-row";
    const runDir = row.lean_run_dir ? "run_dir=present" : "run_dir=-";
    const trackId = row.lean_v2_track_id || "-";
    const prepareStatus = row.lean_v2_prepare_status || "-";
    const prepareIssue = row.lean_prepare_issue_kind || row.lean_prepare_error_class || "-";
    const bottleneckCount = Array.isArray(row.lean_bottlenecks) ? row.lean_bottlenecks.length : 0;
    const handleCount = row.lean_v2_lemma_handles && typeof row.lean_v2_lemma_handles === "object"
      ? Object.keys(row.lean_v2_lemma_handles).length
      : 0;
    const blockedReason = row.blocked_reason || "-";
    const readyForLean = row.ready_for_lean_count ?? 0;
    div.innerHTML = `
      <strong>${row.logical_decomposition_id || row.decomposition_id}</strong><br />
      node=${row.node_id} controller=${row.controller_status} rev=${row.current_revision_number || row.revision_number || 1}/${row.revision_count || 1}<br />
      llm=${row.llm_vetting_status} lean=${row.lean_assembly_status} ${runDir}<br />
      v2_track=${trackId} prepare=${prepareStatus} prepare_issue=${prepareIssue} handles=${handleCount}<br />
      ready_for_lean=${readyForLean} blocked_reason=${blockedReason}<br />
      auto_split=${autoSplitEnabled ? "enabled" : "disabled"} bottlenecks=${bottleneckCount}
    `;
    decList.appendChild(div);
  });
  if (logicalDecompositions.length === 0) {
    decList.innerHTML = '<div class="list-row">No decompositions.</div>';
  }

  const visibleLemmaIds = new Set(state.snapshot.visible_lemma_ids || []);
  const visibleLemmas = state.snapshot.lemmas.filter((row) => visibleLemmaIds.has(row.lemma_id));
  visibleLemmas.forEach((row) => {
    const div = document.createElement("div");
    div.className = "list-row";
    const owner = state.snapshot.lemma_owner_decomposition?.[row.lemma_id] || "-";
    const leanIssue = row.latest_lean_issue_kind || row.latest_lean_issue_class || "-";
    const leanWait = row.lean_wait_reason || "-";
    const confidence = row.latest_lean_result_id && state.snapshot.lean_job_by_id
      ? Object.values(state.snapshot.lean_job_by_id).find((job) => job.target_id === row.lemma_id)?.result?.confidence
      : null;
    div.innerHTML = `
      <strong>${row.lemma_id}</strong><br />
      parent=${row.parent_id} owner_decomposition=${owner}<br />
      proof=${row.proof_status} truth=${row.truth_status || "-"} routing=${row.routing_status} next=${row.next_action || "-"} attempts=${row.solver_attempt_count}<br />
      lean_issue=${leanIssue} lean_wait=${leanWait} confidence=${confidence ?? "-"}
    `;
    lemList.appendChild(div);
  });
  if (visibleLemmas.length === 0) {
    lemList.innerHTML = '<div class="list-row">No approved-decomposition lemmas.</div>';
  }

  const stageRows = state.llmUsage?.by_stage || [];
  if (!stageRows.length) {
    usageList.innerHTML = '<div class="list-row">No OpenAI usage records yet.</div>';
  } else {
    stageRows.forEach((row) => {
      const models = Array.isArray(row.models) && row.models.length ? row.models.join(", ") : "-";
      const div = document.createElement("div");
      div.className = "list-row";
      div.innerHTML = `
        <strong>${row.stage}</strong> calls=${fmtInt(row.call_count)} models=${models}<br />
        in=${fmtInt(row.input_tokens)} cached=${fmtInt(row.cached_input_tokens)} out=${fmtInt(row.output_tokens)} total=${fmtInt(row.total_tokens)}<br />
        cost=${fmtUsd(row.estimated_cost_usd)}
      `;
      usageList.appendChild(div);
    });
  }

  if (state.snapshot.final_proof) {
    finalOutput.textContent = JSON.stringify(state.snapshot.final_proof, null, 2);
  } else if (state.snapshot.nl_only_final_output) {
    finalOutput.textContent = JSON.stringify(state.snapshot.nl_only_final_output, null, 2);
  } else if (state.snapshot.problem?.nl_only_mode) {
    finalOutput.textContent = "NL-only final output will appear here when the problem reaches succeeded.";
  } else {
    finalOutput.textContent = "Final NL output is only available for NL-only succeeded runs.";
  }

  if (state.snapshot.running_final_proof) {
    runningFinalProof.textContent = JSON.stringify(state.snapshot.running_final_proof, null, 2);
  } else if (state.snapshot.legacy_reconstructed_proof) {
    runningFinalProof.textContent = JSON.stringify(state.snapshot.legacy_reconstructed_proof, null, 2);
  } else {
    runningFinalProof.textContent = "Running final proof will appear here once a root proof bundle can be assembled.";
  }

  if (state.problemInput?.input_json) {
    problemInputMeta.textContent = `artifact: ${state.problemInput.artifact_key}`;
    problemInput.textContent = JSON.stringify(state.problemInput.input_json, null, 2);
  } else {
    problemInputMeta.textContent = "Original create payload artifact was not found for this problem.";
    problemInput.textContent = "Problem input JSON is unavailable.";
  }
}

function nodeDetailFor(nodeId) {
  if (!state.snapshot) {
    return null;
  }
  if (state.snapshot.lemma_by_id[nodeId]) {
    return state.snapshot.lemma_by_id[nodeId];
  }
  if (state.snapshot.logical_decomposition_by_id && state.snapshot.logical_decomposition_by_id[nodeId]) {
    return state.snapshot.logical_decomposition_by_id[nodeId];
  }
  if (state.snapshot.decomposition_by_id[nodeId]) {
    return state.snapshot.decomposition_by_id[nodeId];
  }
  if (state.snapshot.lean_job_by_id[nodeId]) {
    return state.snapshot.lean_job_by_id[nodeId];
  }
  if (state.snapshot.final_check_by_id && state.snapshot.final_check_by_id[nodeId]) {
    return state.snapshot.final_check_by_id[nodeId];
  }
  if (state.snapshot.root_theorem && state.snapshot.root_theorem.theorem_id === nodeId) {
    return state.snapshot.root_theorem;
  }
  return null;
}

function proofJsonForNode(nodeId) {
  if (!state.snapshot) {
    return null;
  }
  if (state.snapshot.root_theorem && state.snapshot.root_theorem.theorem_id === nodeId) {
    return (
      state.snapshot.final_proof
      || state.snapshot.running_final_proof
      || state.snapshot.legacy_reconstructed_proof
      || state.snapshot.root_theorem
    );
  }
  if (state.snapshot.lemma_by_id[nodeId]) {
    const row = state.snapshot.lemma_by_id[nodeId];
    return row.proof_bundle || {
      lemma_id: row.lemma_id,
      statement_nl: row.statement_nl,
      proof_nl: row.latest_nl_proof,
      proof_status: row.proof_status,
      truth_status: row.truth_status,
      proof_attempts: row.proof_attempts,
    };
  }
  if (state.snapshot.logical_decomposition_by_id && state.snapshot.logical_decomposition_by_id[nodeId]) {
    const row = state.snapshot.logical_decomposition_by_id[nodeId];
    return row.current_revision?.proof_bundle || row.current_revision || row;
  }
  if (state.snapshot.decomposition_by_id[nodeId]) {
    const row = state.snapshot.decomposition_by_id[nodeId];
    return row.proof_bundle || row.agent6_final_check?.input?.proof_bundle || row;
  }
  if (state.snapshot.final_check_by_id && state.snapshot.final_check_by_id[nodeId]) {
    return state.snapshot.final_check_by_id[nodeId].agent6_final_check?.input || state.snapshot.final_check_by_id[nodeId];
  }
  return nodeDetailFor(nodeId);
}

function statusJsonForNode(nodeId) {
  if (!state.snapshot) {
    return null;
  }
  if (state.snapshot.root_theorem && state.snapshot.root_theorem.theorem_id === nodeId) {
    return {
      problem: state.snapshot.problem,
      root_theorem: state.snapshot.root_theorem,
    };
  }
  if (state.snapshot.lemma_by_id[nodeId]) {
    const row = state.snapshot.lemma_by_id[nodeId];
    return {
      lemma_id: row.lemma_id,
      parent_id: row.parent_id,
      parent_kind: row.parent_kind,
      depth: row.depth,
      statement_status: row.statement_status,
      truth_status: row.truth_status,
      proof_status: row.proof_status,
      routing_status: row.routing_status,
      counterexample_status: row.counterexample_status,
      active_counterexample_id: row.active_counterexample_id,
      active_counterexample: row.active_counterexample,
      solver_attempt_count: row.solver_attempt_count,
      consecutive_fatal_rejections: row.consecutive_fatal_rejections,
      minor_rejection_count: row.minor_rejection_count,
      decomposition_round_count: row.decomposition_round_count,
      materialized_candidate_count: row.materialized_candidate_count,
      promoted_decomposition_count: row.promoted_decomposition_count,
      lean_attempt_count: row.lean_attempt_count,
      lean_identical_fatal_count: row.lean_identical_fatal_count,
      consecutive_infrastructure_failures: row.consecutive_infrastructure_failures,
      solver_series_started_at: row.solver_series_started_at,
      last_submitted_solver_job_id: row.last_submitted_solver_job_id,
      last_submitted_solver_attempt_number: row.last_submitted_solver_attempt_number,
      next_action: row.next_action,
      last_terminal_worker_result: row.last_terminal_worker_result,
      last_transition_reason: row.last_transition_reason,
      proof_bundle_artifact_id: row.proof_bundle_artifact_id,
      latest_vetter_report: row.latest_vetter_report,
      latest_lean_result: row.latest_lean_result,
      latest_lean_result_summary: summarizeLeanResult(row.latest_lean_result),
      latest_lean_issue_class: row.latest_lean_issue_class,
      latest_lean_issue_kind: row.latest_lean_issue_kind,
      lean_wait_reason: row.lean_wait_reason,
      proof_attempts: row.proof_attempts,
      created_at: row.created_at,
      updated_at: row.updated_at,
      last_activity_at: row.last_activity_at,
    };
  }
  if (state.snapshot.logical_decomposition_by_id && state.snapshot.logical_decomposition_by_id[nodeId]) {
    const row = state.snapshot.logical_decomposition_by_id[nodeId];
    return {
      logical_decomposition_id: row.logical_decomposition_id,
      node_id: row.node_id,
      node_kind: row.node_kind,
      controller_status: row.controller_status,
      llm_vetting_status: row.llm_vetting_status,
      lean_assembly_status: row.lean_assembly_status,
      current_revision_id: row.current_revision_id,
      current_revision_number: row.current_revision_number,
      revision_count: row.revision_count,
      current_revision: row.current_revision,
      revisions: row.revisions,
      final_check_passed: row.final_check_passed,
      final_check_job_id: row.final_check_job_id,
      proof_bundle_artifact_id: row.proof_bundle_artifact_id,
      agent6_final_check: row.agent6_final_check,
      created_at: row.created_at,
      updated_at: row.updated_at,
    };
  }
  if (state.snapshot.decomposition_by_id[nodeId]) {
    const row = state.snapshot.decomposition_by_id[nodeId];
    return {
      decomposition_id: row.decomposition_id,
      node_id: row.node_id,
      node_kind: row.node_kind,
      controller_status: row.controller_status,
      llm_vetting_status: row.llm_vetting_status,
      lean_assembly_status: row.lean_assembly_status,
      lean_run_dir: row.lean_run_dir,
      lean_v2_track_id: row.lean_v2_track_id,
      lean_v2_prepare_status: row.lean_v2_prepare_status,
      lean_v2_lemma_handles: row.lean_v2_lemma_handles,
      ready_for_lean_count: row.ready_for_lean_count,
      blocked_reason: row.blocked_reason,
      final_check_passed: row.final_check_passed,
      final_check_job_id: row.final_check_job_id,
      proof_bundle_artifact_id: row.proof_bundle_artifact_id,
      assembly_plan: row.assembly_plan,
      agent3_vetter_output: row.agent3_vetter_output,
      agent6_final_check: row.agent6_final_check,
      created_at: row.created_at,
      updated_at: row.updated_at,
    };
  }
  if (state.snapshot.lean_job_by_id && state.snapshot.lean_job_by_id[nodeId]) {
    const row = state.snapshot.lean_job_by_id[nodeId];
    return {
      job_id: row.job_id,
      target_id: row.target_id,
      target_kind: row.target_kind,
      mode: row.mode,
      operation: row.operation,
      status: row.status,
      attempt_index: row.attempt_index,
      progress_snapshot: row.progress_snapshot,
      issue_kind: row.issue_kind,
      confidence: row.confidence,
      fatality: row.fatality,
      last_error: row.last_error,
      request_artifact_id: row.request_artifact_id,
      result_artifact_id: row.result_artifact_id,
      result: row.result,
      created_at: row.created_at,
      updated_at: row.updated_at,
    };
  }
  if (state.snapshot.final_check_by_id && state.snapshot.final_check_by_id[nodeId]) {
    return state.snapshot.final_check_by_id[nodeId];
  }
  return nodeDetailFor(nodeId);
}

function leanMetadataForNode(nodeId) {
  if (!state.snapshot) {
    return null;
  }
  if (state.snapshot.lemma_by_id[nodeId]) {
    const row = state.snapshot.lemma_by_id[nodeId];
    return {
      node_kind: "lemma",
      node_id: row.lemma_id,
      latest_job: row.latest_formalize_job || null,
      lean_compile_summary: row.lean_compile_summary || null,
      timing_summary: row.timing_summary || null,
      blocked_reason: row.lean_wait_reason || null,
    };
  }
  if (state.snapshot.decomposition_by_id[nodeId]) {
    const row = state.snapshot.decomposition_by_id[nodeId];
    return {
      node_kind: "decomposition",
      node_id: row.decomposition_id,
      latest_job: row.latest_prepare_track_job || null,
      lean_compile_summary: row.lean_compile_summary || null,
      timing_summary: row.timing_summary || null,
      blocked_reason: row.blocked_reason || null,
      ready_for_lean_count: row.ready_for_lean_count || 0,
    };
  }
  if (state.snapshot.lean_job_by_id && state.snapshot.lean_job_by_id[nodeId]) {
    const row = state.snapshot.lean_job_by_id[nodeId];
    return {
      node_kind: "lean_job",
      node_id: row.job_id,
      latest_job: row,
      lean_compile_summary: row.lean_compile_summary || null,
      timing_summary: row.timing_summary || null,
    };
  }
  return null;
}

function leanCacheKey(nodeId) {
  return [
    state.currentProblemId || "",
    nodeId || "",
    state.snapshot?.problem?.updated_at || state.snapshot?.problem?.problem_id || "",
  ].join(":");
}

async function fetchLeanFilesForNode(nodeId) {
  if (!state.currentProblemId || !nodeId) {
    return null;
  }
  const cacheKey = leanCacheKey(nodeId);
  if (state.leanFileCache[cacheKey]) {
    return state.leanFileCache[cacheKey];
  }
  state.leanFileCache[cacheKey] = { loading: true, problem_id: state.currentProblemId, node_id: nodeId, entries: [] };
  try {
    const payload = await api(`/v1/debug/problems/${state.currentProblemId}/lean-files/${encodeURIComponent(nodeId)}`);
    state.leanFileCache[cacheKey] = payload;
  } catch (error) {
    state.leanFileCache[cacheKey] = {
      error: error.message,
      problem_id: state.currentProblemId,
      node_id: nodeId,
      entries: [],
    };
  }
  return state.leanFileCache[cacheKey];
}

function formatLeanEntry(entry) {
  const lines = [];
  lines.push(`${entry.label} [${entry.kind}]`);
  lines.push(`path: ${entry.path || "-"}`);
  lines.push(`exists: ${entry.exists ? "yes" : "no"}`);
  lines.push(`compile_status: ${entry.compile_status || "-"}`);
  lines.push(`source_job_id: ${entry.source_job_id || "-"}`);
  lines.push(`updated_at: ${entry.updated_at || "-"}`);
  if (Array.isArray(entry.compile_diagnostics) && entry.compile_diagnostics.length) {
    lines.push("compile_diagnostics:");
    entry.compile_diagnostics.forEach((item) => {
      lines.push(`- ${JSON.stringify(item)}`);
    });
  }
  lines.push("");
  lines.push(entry.content || "<file content unavailable>");
  return lines.join("\n");
}

function formatLeanDetail(nodeId, leanPayload) {
  const meta = leanMetadataForNode(nodeId);
  const lines = [];
  if (meta) {
    lines.push(`node_kind: ${meta.node_kind}`);
    lines.push(`node_id: ${meta.node_id}`);
    if (meta.latest_job) {
      lines.push(`latest_job_id: ${meta.latest_job.job_id || "-"}`);
      lines.push(`latest_job_status: ${meta.latest_job.status || "-"}`);
      lines.push(`latest_job_operation: ${meta.latest_job.operation || meta.latest_job.mode || "-"}`);
    }
    const compile = meta.lean_compile_summary || {};
    lines.push(`artifact_check_status: ${compile.artifact_check_status || "-"}`);
    lines.push(`integration_check_status: ${compile.integration_check_status || "-"}`);
    const timing = meta.timing_summary || {};
    lines.push(`queue_duration: ${fmtDurationSeconds(timing.queue_duration_seconds)}`);
    lines.push(`service_run_duration: ${fmtDurationSeconds(timing.service_run_duration_seconds)}`);
    lines.push(`controller_handoff_lag: ${fmtDurationSeconds(timing.controller_handoff_lag_seconds)}`);
    lines.push(`end_to_end_duration: ${fmtDurationSeconds(timing.end_to_end_duration_seconds)}`);
    if (meta.blocked_reason) {
      lines.push(`blocked_reason: ${meta.blocked_reason}`);
    }
    if (meta.ready_for_lean_count !== undefined) {
      lines.push(`ready_for_lean_count: ${meta.ready_for_lean_count}`);
    }
  }
  if (leanPayload?.error) {
    lines.push("");
    lines.push(`lean_file_load_error: ${leanPayload.error}`);
  }
  const entries = Array.isArray(leanPayload?.entries) ? leanPayload.entries : [];
  if (!entries.length) {
    lines.push("");
    lines.push("No Lean files available for this node.");
    return lines.join("\n");
  }
  entries.forEach((entry, index) => {
    lines.push("");
    lines.push(`=== File ${index + 1} ===`);
    lines.push(formatLeanEntry(entry));
  });
  return lines.join("\n");
}

function renderSelectedTreeNodeDetail() {
  const detailHost = byId("tree-node-detail");
  const proofTab = byId("tree-detail-proof-tab");
  const statusTab = byId("tree-detail-status-tab");
  const leanTab = byId("tree-detail-lean-tab");
  const activeTab = state.selectedTreeDetailTab === "status"
    ? "status"
    : state.selectedTreeDetailTab === "lean"
      ? "lean"
      : "proof";
  proofTab.classList.toggle("primary", activeTab === "proof");
  statusTab.classList.toggle("primary", activeTab === "status");
  leanTab.classList.toggle("primary", activeTab === "lean");
  if (!state.selectedTreeNodeId) {
    detailHost.textContent = "Select a node.";
    return;
  }
  if (activeTab === "lean") {
    const cacheKey = leanCacheKey(state.selectedTreeNodeId);
    const payload = state.leanFileCache[cacheKey];
    if (!payload) {
      detailHost.textContent = "Loading Lean files...";
      void fetchLeanFilesForNode(state.selectedTreeNodeId).then(() => {
        if (state.selectedTreeDetailTab === "lean" && state.selectedTreeNodeId) {
          renderSelectedTreeNodeDetail();
        }
      });
      return;
    }
    if (payload.loading) {
      detailHost.textContent = "Loading Lean files...";
      return;
    }
    detailHost.textContent = formatLeanDetail(state.selectedTreeNodeId, payload);
    return;
  }
  const detail = activeTab === "status"
    ? statusJsonForNode(state.selectedTreeNodeId)
    : proofJsonForNode(state.selectedTreeNodeId);
  detailHost.textContent = detail ? JSON.stringify(detail, null, 2) : "Selected node is no longer available.";
}

function rootTrackRows(snapshot) {
  if (!snapshot?.root_theorem?.theorem_id) {
    return [];
  }
  const rootId = snapshot.root_theorem.theorem_id;
  const configuredTrackIds =
    Array.isArray(snapshot.root_track_decomposition_ids) && snapshot.root_track_decomposition_ids.length
      ? new Set(snapshot.root_track_decomposition_ids)
      : null;
  return (snapshot.decompositions || [])
    .filter(
      (row) =>
        row.node_id === rootId &&
        row.llm_vetting_status === "accepted" &&
        (!configuredTrackIds || configuredTrackIds.has(row.decomposition_id)),
    )
    .sort((a, b) => {
      const ta = Date.parse(a.created_at || 0);
      const tb = Date.parse(b.created_at || 0);
      if (ta !== tb) {
        return ta - tb;
      }
      return String(a.decomposition_id || "").localeCompare(String(b.decomposition_id || ""));
    });
}

function syncRootTrackSelector(snapshot) {
  const select = byId("tree-root-track");
  const tracks = rootTrackRows(snapshot);
  select.innerHTML = "";

  const allOption = document.createElement("option");
  allOption.value = "all";
  allOption.textContent = "all root tracks";
  select.appendChild(allOption);

  tracks.forEach((row) => {
    const option = document.createElement("option");
    option.value = row.decomposition_id;
    option.textContent = `${row.decomposition_id} [${row.controller_status}]`;
    select.appendChild(option);
  });

  const valid = new Set(["all", ...tracks.map((row) => row.decomposition_id)]);
  if (!valid.has(state.selectedRootTrackId)) {
    state.selectedRootTrackId = "all";
  }
  select.value = state.selectedRootTrackId;
  select.disabled = tracks.length <= 1;
  return tracks;
}

function filteredTreeGraph(snapshot) {
  const graph = snapshot?.node_graph || { nodes: [], edges: [] };
  if (state.selectedRootTrackId === "all") {
    return graph;
  }

  const targetRootTrack = state.selectedRootTrackId;
  const theoremId = snapshot?.root_theorem?.theorem_id || null;
  const lemmaOwner = snapshot?.lemma_owner_decomposition || {};
  const lemmas = Array.isArray(snapshot?.lemmas) ? snapshot.lemmas : [];
  const childrenByFrom = {};
  graph.edges.forEach((edge) => {
    const fromId = edge.from;
    if (!childrenByFrom[fromId]) {
      childrenByFrom[fromId] = [];
    }
    childrenByFrom[fromId].push(edge.to);
  });

  const include = new Set([targetRootTrack]);
  if (theoremId) {
    include.add(theoremId);
  }
  const queue = [targetRootTrack];

  const ownedRootLemmaIds = lemmas
    .filter(
      (row) =>
        row &&
        row.parent_id === theoremId &&
        lemmaOwner[row.lemma_id] === targetRootTrack,
    )
    .map((row) => row.lemma_id);
  ownedRootLemmaIds.forEach((lemmaId) => {
    if (!include.has(lemmaId)) {
      include.add(lemmaId);
      queue.push(lemmaId);
    }
  });

  while (queue.length) {
    const current = queue.shift();
    (childrenByFrom[current] || []).forEach((childId) => {
      if (!include.has(childId)) {
        include.add(childId);
        queue.push(childId);
      }
    });
  }

  const filteredEdges = graph.edges.filter((edge) => include.has(edge.from) && include.has(edge.to));
  const trimmedEdges = theoremId
    ? filteredEdges.filter(
        (edge) => !(edge.from === theoremId && lemmaOwner[edge.to] === targetRootTrack),
      )
    : filteredEdges;

  ownedRootLemmaIds.forEach((lemmaId) => {
    if (!include.has(lemmaId)) {
      return;
    }
    const exists = trimmedEdges.some((edge) => edge.from === targetRootTrack && edge.to === lemmaId);
    if (!exists) {
      trimmedEdges.push({
        from: targetRootTrack,
        to: lemmaId,
        relation: "contains",
      });
    }
  });

  return {
    nodes: graph.nodes.filter((node) => include.has(node.id)),
    edges: trimmedEdges,
  };
}

function renderTreeExplorer() {
  const host = byId("tree-explorer");
  host.innerHTML = "";
  if (!state.snapshot) {
    byId("tree-root-track").innerHTML = '<option value="all">all root tracks</option>';
    byId("tree-root-track").value = "all";
    byId("tree-root-track").disabled = true;
    host.innerHTML = '<div class="list-row">No problem selected.</div>';
    return;
  }

  syncRootTrackSelector(state.snapshot);
  const graph = filteredTreeGraph(state.snapshot);
  const nodesById = {};
  graph.nodes.forEach((n) => {
    nodesById[n.id] = n;
  });
  const children = {};
  const hasParent = new Set();
  graph.edges.forEach((e) => {
    const fromId = e.from;
    if (!children[fromId]) {
      children[fromId] = [];
    }
    children[fromId].push(e.to);
    hasParent.add(e.to);
  });

  const roots = graph.nodes
    .filter((node) => !hasParent.has(node.id))
    .map((node) => node.id);

  const sortIds = (ids) =>
    [...ids].sort((a, b) => {
      const ka = nodesById[a]?.kind || "";
      const kb = nodesById[b]?.kind || "";
      const order = { theorem: 0, decomposition: 1, lemma: 2, lean_job: 3 };
      const oa = order[ka] ?? 99;
      const ob = order[kb] ?? 99;
      if (oa !== ob) {
        return oa - ob;
      }
      return a.localeCompare(b);
    });

  function renderNode(nodeId) {
    const node = nodesById[nodeId];
    const li = document.createElement("li");
    li.className = "tree-node";
    const btn = document.createElement("button");
    btn.type = "button";
    let label = `${node.kind} ${node.id} [${node.status}]`;
    if (node.kind === "decomposition") {
      const origin = node.metadata?.decomposition_origin;
      if (origin) {
        label += ` <${origin}>`;
      }
    }
    if (node.kind === "lean_job") {
      const issue = node.metadata?.issue_kind;
      const fatality = node.metadata?.fatality;
      if (issue) {
        label += ` <${issue}>`;
      }
      if (fatality) {
        label += ` (${fatality})`;
      }
    }
    btn.textContent = label;
    if (state.selectedTreeNodeId === nodeId) {
      btn.classList.add("primary");
    }
    btn.addEventListener("click", () => {
      state.selectedTreeNodeId = nodeId;
      renderTreeExplorer();
      renderSelectedTreeNodeDetail();
    });
    li.appendChild(btn);
    const childIds = sortIds(children[nodeId] || []);
    if (childIds.length > 0) {
      const ul = document.createElement("ul");
      childIds.forEach((childId) => ul.appendChild(renderNode(childId)));
      li.appendChild(ul);
    }
    return li;
  }

  const ul = document.createElement("ul");
  sortIds(roots).forEach((rootId) => ul.appendChild(renderNode(rootId)));
  host.appendChild(ul);

  if (state.selectedTreeNodeId && !nodeDetailFor(state.selectedTreeNodeId)) {
    byId("tree-node-detail").textContent = "Selected node is no longer available.";
  }
  renderSelectedTreeNodeDetail();
}

function requestArtifactsByPrefix(prefix) {
  return state.requestArtifacts.filter((row) => row.artifact_key.startsWith(prefix));
}

function sortArtifactKeys(keys) {
  const rowByKey = new Map(state.requestArtifacts.map((row) => [row.artifact_key, row]));
  return [...keys].sort((a, b) => {
    const ta = Date.parse(rowByKey.get(a)?.modified_at || 0);
    const tb = Date.parse(rowByKey.get(b)?.modified_at || 0);
    if (ta !== tb) {
      return tb - ta;
    }
    return a.localeCompare(b);
  });
}

function parseAttemptFromKey(key) {
  const m = key.match(/attempt_(\d+)/);
  return m ? Number.parseInt(m[1], 10) : -1;
}

function pickPreferredOutputKey(keys) {
  if (!keys.length) {
    return null;
  }
  const parsed = keys
    .filter((key) => key.includes("_parsed_output_attempt_"))
    .sort((a, b) => parseAttemptFromKey(b) - parseAttemptFromKey(a));
  if (parsed.length) {
    return parsed[0];
  }

  const terminalResponse = keys.filter((key) => key.endsWith("/response.json"));
  if (terminalResponse.length) {
    return sortArtifactKeys(terminalResponse)[0];
  }

  const submitResponse = keys.filter((key) => key.endsWith("/submit_response.json"));
  if (submitResponse.length) {
    return sortArtifactKeys(submitResponse)[0];
  }

  const raw = keys
    .filter((key) => key.includes("_raw_output_attempt_"))
    .sort((a, b) => parseAttemptFromKey(b) - parseAttemptFromKey(a));
  if (raw.length) {
    return raw[0];
  }
  return sortArtifactKeys(keys)[0];
}

function buildRequestInspectorRelated(entry) {
  const related = {
    input: [],
    prompt: [],
    output: [],
    failure: [],
    all: [],
  };
  const add = (bucket, key) => {
    if (!key || typeof key !== "string") {
      return;
    }
    if (!related[bucket].includes(key)) {
      related[bucket].push(key);
    }
    if (!related.all.includes(key)) {
      related.all.push(key);
    }
  };

  const keySet = new Set(state.requestArtifacts.map((row) => row.artifact_key));
  add("input", entry.artifact_key);

  if (entry.source === "api_create") {
    const responseKey = `problems/${state.currentProblemId}/api/problem_create_response.json`;
    if (keySet.has(responseKey)) {
      add("output", responseKey);
    }
  } else if (entry.source === "api_run" || entry.source === "api_start" || entry.source === "api_pause" || entry.source === "api_resume") {
    const responseKey = entry.artifact_key.replace(".request.json", ".response.json");
    if (keySet.has(responseKey)) {
      add("output", responseKey);
    }
  } else if (entry.source.startsWith("agent")) {
    const prefix = entry.artifact_key.slice(0, entry.artifact_key.lastIndexOf("/"));
    const rows = requestArtifactsByPrefix(`${prefix}/`);
    rows.forEach((row) => {
      const key = row.artifact_key;
      if (key.endsWith(`/${entry.source}_system_prompt.txt`)) {
        add("prompt", key);
        return;
      }
      if (key.includes(`/${entry.source}_parsed_output_attempt_`) || key.includes(`/${entry.source}_raw_output_attempt_`)) {
        add("output", key);
        return;
      }
      if (key.includes(`/${entry.source}_request_error_attempt_`) || key.endsWith(`/${entry.source}_parse_error.json`)) {
        add("failure", key);
      }
    });
  } else if (entry.source === "lean") {
    const prefix = entry.artifact_key.slice(0, entry.artifact_key.lastIndexOf("/"));
    const rows = requestArtifactsByPrefix(`${prefix}/`);
    rows.forEach((row) => {
      const key = row.artifact_key;
      if (key.endsWith("/request.json")) {
        add("input", key);
        return;
      }
      if (key.endsWith("/submit_response.json") || key.endsWith("/response.json") || key.endsWith("/result.json")) {
        add("output", key);
      }
    });
  }

  related.input = sortArtifactKeys(related.input);
  related.prompt = sortArtifactKeys(related.prompt);
  related.output = sortArtifactKeys(related.output);
  related.failure = sortArtifactKeys(related.failure);
  related.all = sortArtifactKeys(related.all);
  return related;
}

function classifyRequestArtifact(key, related) {
  if (related.input.includes(key)) {
    return "input";
  }
  if (related.prompt.includes(key)) {
    return "prompt";
  }
  if (related.output.includes(key)) {
    return "output";
  }
  if (related.failure.includes(key)) {
    return "failure";
  }
  return "other";
}

function requestEntryById(entryId, artifactKey) {
  if (entryId) {
    const byIdMatch = state.requestLog.find((entry) => entry.entry_id === entryId);
    if (byIdMatch) {
      return byIdMatch;
    }
  }
  if (artifactKey) {
    return state.requestLog.find((entry) => entry.artifact_key === artifactKey) || null;
  }
  return null;
}

function selectRequestEntry(entry) {
  state.selectedRequestEntryId = entry.entry_id;
  state.selectedRequestArtifactKey = entry.artifact_key;
  const related = buildRequestInspectorRelated(entry);
  const selectedView =
    entry.completion_status === "failed" && (related.failure.length > 0 || entry.completion_detail) ? "failure" : "input";
  state.requestInspector = {
    entry,
    related,
    selectedView,
    selectedKeyByView: {
      input: related.input[0] || null,
      prompt: related.prompt[0] || null,
      output: pickPreferredOutputKey(related.output),
      failure: related.failure[0] || null,
    },
  };
}

async function loadRequestInspectorArtifactContent(artifactKey) {
  if (!artifactKey) {
    return null;
  }
  if (state.requestArtifactContentCache[artifactKey]) {
    return state.requestArtifactContentCache[artifactKey];
  }
  const path = encodeArtifactPath(artifactKey);
  const payload = await api(`/v1/debug/artifacts/${path}`);
  state.requestArtifactContentCache[artifactKey] = payload;
  return payload;
}

async function renderRequestInspectorDetail() {
  const detailHost = byId("request-log-detail");
  if (!state.requestInspector) {
    detailHost.textContent = "Select a request entry.";
    return;
  }
  const { entry, related, selectedView, selectedKeyByView } = state.requestInspector;
  const selectedArtifact = selectedKeyByView[selectedView] || null;

  if (selectedView === "failure" && !selectedArtifact) {
    detailHost.textContent = entry.completion_detail || "No failure artifact found for this request.";
    return;
  }
  if (!selectedArtifact) {
    detailHost.textContent = `No ${selectedView} artifact found for this request.`;
    return;
  }

  detailHost.textContent = `Loading ${selectedView} artifact...`;
  try {
    const payload = await loadRequestInspectorArtifactContent(selectedArtifact);
    if (state.requestInspector?.entry?.artifact_key !== entry.artifact_key) {
      return;
    }

    if (selectedView === "failure" && entry.completion_detail) {
      const rendered = payload.format === "json" ? JSON.stringify(payload.content, null, 2) : String(payload.content);
      detailHost.textContent = `${entry.completion_detail}\n\n${rendered}`;
      return;
    }
    detailHost.textContent = payload.format === "json" ? JSON.stringify(payload.content, null, 2) : String(payload.content);
  } catch (error) {
    if (state.requestInspector?.entry?.artifact_key !== entry.artifact_key) {
      return;
    }
    detailHost.textContent = `Error loading selected artifact: ${error.message}`;
  }
}

function renderRequestInspector() {
  const metaHost = byId("request-inspector-meta");
  const viewSelect = byId("request-inspector-view");
  const artifactSelect = byId("request-inspector-artifact");
  const relatedHost = byId("request-related-artifacts");
  relatedHost.innerHTML = "";

  if (!state.currentProblemId || !state.requestInspector) {
    metaHost.textContent = "Select a request entry.";
    artifactSelect.innerHTML = "";
    byId("request-log-detail").textContent = "Select a request entry.";
    return;
  }

  const { entry, related, selectedView, selectedKeyByView } = state.requestInspector;
  const interrupted = (entry.completion_detail || "").toLowerCase().includes("interrupted");
  const responseIdLine = entry.response_id ? `<br />response_id=${entry.response_id}` : "";
  const providerTerminalStatusLine = entry.provider_terminal_status
    ? `<br />provider_terminal_status=${entry.provider_terminal_status}`
    : "";
  const requestConfigLine = `<br />model=${entry.llm_model || "-"} reasoning=${entry.llm_reasoning_effort || "-"}`;
  metaHost.innerHTML = `
    source=<strong>${entry.source}</strong> time=${entry.timestamp}
    <span class="pill status-${entry.completion_status}">${entry.completion_status}</span><br />
    ${interrupted ? '<span class="pill status-interrupted">interrupted</span><br />' : ""}
    state=${entry.completion_status}<br />
    target=${entry.target_id || "-"}<br />
    ${entry.summary}${requestConfigLine}${entry.completion_detail ? `<br />detail=${entry.completion_detail}` : ""}${responseIdLine}${providerTerminalStatusLine}
    ${
      entry.llm_call_count > 0
        ? `<br />llm_calls=${fmtInt(entry.llm_call_count)} in=${fmtInt(entry.llm_input_tokens)} cached=${fmtInt(
            entry.llm_cached_input_tokens
          )} out=${fmtInt(entry.llm_output_tokens)} total=${fmtInt(entry.llm_total_tokens)} cost=${fmtUsd(
            entry.llm_estimated_cost_usd
          )}`
        : ""
    }
  `;

  viewSelect.value = selectedView;
  const keysForView = related[selectedView] || [];
  artifactSelect.innerHTML = "";
  if (!keysForView.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = `No ${selectedView} artifact`;
    artifactSelect.appendChild(option);
  } else {
    keysForView.forEach((key) => {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = key;
      artifactSelect.appendChild(option);
    });
  }
  artifactSelect.value = selectedKeyByView[selectedView] || artifactSelect.value;

  if (!related.all.length) {
    relatedHost.innerHTML = '<div class="list-row muted">No related artifacts detected.</div>';
    return;
  }

  related.all.forEach((key) => {
    const type = classifyRequestArtifact(key, related);
    const row = document.createElement("div");
    row.className = "list-row selectable";
    if (selectedKeyByView[selectedView] === key) {
      row.classList.add("selected");
    }
    row.innerHTML = `<strong>${type}</strong><br /><span class="small">${key}</span>`;
    row.addEventListener("click", () => {
      state.requestInspector.selectedView = type === "other" ? "output" : type;
      if (!state.requestInspector.selectedKeyByView[state.requestInspector.selectedView]) {
        state.requestInspector.selectedKeyByView[state.requestInspector.selectedView] = key;
      } else {
        state.requestInspector.selectedKeyByView[state.requestInspector.selectedView] = key;
      }
      renderRequestInspector();
      void renderRequestInspectorDetail();
    });
    relatedHost.appendChild(row);
  });
}

function renderRequestLog() {
  const host = byId("request-log-list");
  host.innerHTML = "";
  if (!state.currentProblemId) {
    host.innerHTML = '<div class="list-row">No problem selected.</div>';
    return;
  }
  const sourceFilter = byId("request-source-filter").value;
  if (state.requestLog.length === 0) {
    host.innerHTML = '<div class="list-row">No request log entries found.</div>';
    return;
  }

  const selectedEntry = requestEntryById(state.selectedRequestEntryId, state.selectedRequestArtifactKey);
  if (selectedEntry && (!state.requestInspector || state.requestInspector.entry.artifact_key !== selectedEntry.artifact_key)) {
    selectRequestEntry(selectedEntry);
  }
  if (!selectedEntry && state.selectedRequestArtifactKey) {
    state.selectedRequestEntryId = null;
    state.selectedRequestArtifactKey = null;
    state.requestInspector = null;
  }

  state.requestLog.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "list-row selectable";
    if (state.selectedRequestArtifactKey === entry.artifact_key) {
      row.classList.add("selected");
    }
    const interrupted = (entry.completion_detail || "").toLowerCase().includes("interrupted");
    const completionDetail = entry.completion_detail ? `<br /><span class="small">${entry.completion_detail}</span>` : "";
    const responseIdDetail = entry.response_id ? `<br /><span class="small">response_id=${entry.response_id}</span>` : "";
    const providerTerminalStatusDetail = entry.provider_terminal_status
      ? `<br /><span class="small">provider_terminal_status=${entry.provider_terminal_status}</span>`
      : "";
    const requestConfigDetail = `<br /><span class="small">model=${entry.llm_model || "-"} reasoning=${
      entry.llm_reasoning_effort || "-"
    }</span>`;
    const llmDetail =
      entry.llm_call_count > 0
        ? `<br /><span class="small">llm_calls=${fmtInt(entry.llm_call_count)} in=${fmtInt(
            entry.llm_input_tokens
          )} cached=${fmtInt(entry.llm_cached_input_tokens)} out=${fmtInt(entry.llm_output_tokens)} total=${fmtInt(
            entry.llm_total_tokens
          )} cost=${fmtUsd(entry.llm_estimated_cost_usd)} models=${
            entry.llm_models && entry.llm_models.length ? entry.llm_models.join(", ") : "-"
          }</span>`
        : "";
    row.innerHTML = `
      <strong>${entry.source}</strong> ${entry.timestamp}
      <span class="pill status-${entry.completion_status}">${entry.completion_status}</span><br />
      ${interrupted ? '<span class="pill status-interrupted">interrupted</span><br />' : ""}
      state=${entry.completion_status}<br />
      target=${entry.target_id || "-"}<br />
      ${entry.summary}<br />
      ${requestConfigDetail}
      ${completionDetail}
      ${responseIdDetail}
      ${providerTerminalStatusDetail}
      ${llmDetail}
      <br />
      <span class="small">${entry.artifact_key}</span>
    `;
    row.addEventListener("click", () => {
      selectRequestEntry(entry);
      renderRequestLog();
      renderRequestInspector();
      void renderRequestInspectorDetail();
    });
    host.appendChild(row);
  });

  renderRequestInspector();
  void renderRequestInspectorDetail();
}

function renderArtifacts() {
  const host = byId("artifacts-view");
  host.innerHTML = "";
  if (!state.currentProblemId) {
    host.innerHTML = '<div class="list-row">No problem selected.</div>';
    return;
  }
  const search = byId("artifact-search").value.trim().toLowerCase();
  const rows = state.artifacts.filter((row) => !search || row.artifact_key.toLowerCase().includes(search));
  if (rows.length === 0) {
    host.innerHTML = '<div class="list-row">No artifact matches current filters.</div>';
    return;
  }
  rows.forEach((row) => {
    const div = document.createElement("div");
    div.className = "list-row selectable";
    if (state.selectedArtifactKey === row.artifact_key) {
      div.classList.add("selected");
    }
    div.innerHTML = `<strong>${row.source}</strong> ${row.size_bytes} bytes<br /><span class="small">${row.artifact_key}</span>`;
    div.addEventListener("click", async () => {
      state.selectedArtifactKey = row.artifact_key;
      renderArtifacts();
      try {
        const path = encodeArtifactPath(row.artifact_key);
        const payload = await api(`/v1/debug/artifacts/${path}`);
        const content = payload.format === "json" ? JSON.stringify(payload.content, null, 2) : String(payload.content);
        byId("artifact-content").textContent = content;
      } catch (error) {
        byId("artifact-content").textContent = `Artifact load error: ${error.message}`;
      }
    });
    host.appendChild(div);
  });
}

function renderEvents() {
  const host = byId("events-view");
  host.innerHTML = "";
  if (!state.snapshot) {
    host.innerHTML = '<div class="list-row">No problem selected.</div>';
    return;
  }
  const stageFilter = byId("event-stage-filter").value.trim().toLowerCase();
  const textFilter = byId("event-text-filter").value.trim().toLowerCase();

  const rows = state.snapshot.events.filter((row) => {
    if (stageFilter && !row.stage.toLowerCase().includes(stageFilter)) {
      return false;
    }
    if (textFilter) {
      const haystack = `${row.reason || ""} ${row.target_node_id || ""} ${row.worker_job_id || ""}`.toLowerCase();
      if (!haystack.includes(textFilter)) {
        return false;
      }
    }
    return true;
  });

  if (rows.length === 0) {
    host.innerHTML = '<div class="list-row">No events for current filters.</div>';
    return;
  }

  rows.forEach((row) => {
    const div = document.createElement("div");
    div.className = "list-row";
    div.innerHTML = `
      <strong>#${row.event_id}</strong> ${row.stage}<br />
      target=${row.target_node_id || "-"} status=${row.new_status || "-"} job=${row.worker_job_id || "-"}<br />
      <span class="small">${row.reason || ""}</span>
    `;
    host.appendChild(div);
  });
}

function renderRawData() {
  byId("raw-snapshot").textContent = state.snapshot ? JSON.stringify(state.snapshot, null, 2) : "";
  byId("raw-progress").textContent = state.progress ? JSON.stringify(state.progress, null, 2) : "";
}

function renderAll() {
  renderSubmitPageContext();
  renderProblemBadges();
  renderOverview();
  renderTreeExplorer();
  renderRequestLog();
  renderArtifacts();
  renderEvents();
  renderRawData();
}

async function refreshCurrentProblem() {
  if (!state.currentProblemId) {
    state.snapshot = null;
    state.progress = null;
    state.costSummary = null;
    state.llmUsage = null;
    state.problemInput = null;
    state.requestLog = [];
    state.requestArtifacts = [];
    state.selectedRootTrackId = "all";
    state.leanFileCache = {};
    state.requestInspector = null;
    state.requestArtifactContentCache = {};
    state.artifacts = [];
    renderAll();
    return;
  }
  await Promise.all([
    refreshProblemList(),
    fetchSnapshot(),
    fetchProgress(),
    fetchCostSummary(),
    fetchLlmUsageSummary(),
    fetchProblemInput(),
    fetchRequestLog(),
    fetchRequestArtifacts(),
    fetchArtifacts(),
  ]);
  syncAutoRunWithProblemStatus();
  // Keep Continue button state in sync even when we skip full re-render due
  // to active text selection in the UI.
  updateContinueButtonState();
  updatePauseButtonState();
  // Skip re-render if user has active text selection to avoid clearing it
  const sel = window.getSelection();
  if (sel && sel.toString().length > 0) {
    return;
  }
  renderAll();
}

async function loadProblem(problemId, pageName = "problem") {
  state.currentProblemId = problemId || null;
  state.pauseHold = false;
  setProblemInUrl(state.currentProblemId);
  setPageInUrl(pageName);
  state.selectedTreeNodeId = null;
  state.selectedTreeDetailTab = "proof";
  state.selectedRootTrackId = "all";
  state.leanFileCache = {};
  state.selectedRequestEntryId = null;
  state.selectedRequestArtifactKey = null;
  state.requestInspector = null;
  state.requestArtifactContentCache = {};
  state.selectedArtifactKey = null;
  state.eventsLimit = 250;
  byId("continue-problem").disabled = true;
  byId("pause-problem").disabled = true;
  byId("download-logs").disabled = !state.currentProblemId;
  byId("delete-problem").disabled = !state.currentProblemId;
  if (!state.currentProblemId) {
    stopAutoRun();
    closeEventStream();
    state.nextReconcileAtMs = 0;
    stopLiveRefresh();
    await refreshCurrentProblem();
    activatePage(pageName);
    return;
  }
  byId("artifact-prefix").value = `problems/${state.currentProblemId}`;
  try {
    await reconcileIncompleteRuns();
  } catch (error) {
    setMessage(`Reconcile warning: ${error.message}`);
  }
  state.nextReconcileAtMs = Date.now() + 30000;
  await refreshCurrentProblem();
  if (pageName === "problem") {
    openEventStream(state.currentProblemId);
    startLiveRefresh();
    syncAutoRunWithProblemStatus();
  } else {
    stopAutoRun();
    closeEventStream();
    stopLiveRefresh();
  }
  activatePage(pageName);
}

function showSubmitPage() {
  stopAutoRun();
  closeEventStream();
  stopLiveRefresh();
  activatePage("submit");
  renderAll();
}

async function continueCurrentProblem() {
  if (!state.currentProblemId || state.controlInFlight) {
    return;
  }
  const status = state.snapshot?.problem?.status;
  if (!status || status === "succeeded") {
    updateContinueButtonState();
    return;
  }
  state.controlInFlight = true;
  updateContinueButtonState();
  updatePauseButtonState();
  try {
    let payload = null;
    let continueMode = "";
    if (status === "failed") {
      try {
        payload = await api(
          `/v1/debug/problems/${state.currentProblemId}/resume-after-infrastructure-failure`,
          { method: "POST" },
        );
        continueMode = "infra-resume";
      } catch (error) {
        const code = error?.body?.error?.code;
        if (error?.status !== 409 || code !== "not_infrastructure_failure") {
          throw error;
        }
        payload = await api(`/v1/problems/${state.currentProblemId}/resume`, { method: "POST" });
        continueMode = "resume";
      }
    } else {
      payload = await api(`/v1/problems/${state.currentProblemId}/start`, {
        method: "POST",
        headers: { "X-Debug-Run-Trigger": "continue_button" },
      });
      continueMode = "start";
    }
    state.pauseHold = false;
    const executionId = payload?.execution?.execution_id || payload?.execution_id || "-";
    const generation = payload?.execution?.continuation_generation ?? "-";
    if (continueMode === "infra-resume") {
      setMessage(
        `Continued via infra-resume. generation=${generation} execution=${executionId} lemma=${payload?.resumed_lemma_id || "-"}`,
      );
    } else if (continueMode === "resume") {
      setMessage(`Continued via resume. generation=${generation} execution=${executionId}`);
    } else {
      setMessage(`Continued via start. status=${payload?.status || "-"} generation=${generation} execution=${executionId}`);
    }
    await refreshCurrentProblem();
  } catch (error) {
    setMessage(`Continue error: ${error.message}`);
  } finally {
    state.controlInFlight = false;
    updateContinueButtonState();
    updatePauseButtonState();
    syncAutoRunWithProblemStatus();
  }
}

async function pauseCurrentProblem() {
  if (!state.currentProblemId || state.controlInFlight) {
    return;
  }
  state.controlInFlight = true;
  state.pauseHold = true;
  stopAutoRun();
  updateContinueButtonState();
  updatePauseButtonState();
  try {
    const payload = await api(`/v1/problems/${state.currentProblemId}/pause`, { method: "POST" });
    const executionId = payload?.execution?.execution_id || "-";
    const generation = payload?.execution?.continuation_generation ?? "-";
    setMessage(`Paused problem. status=${payload?.status || "-"} generation=${generation} execution=${executionId}`);
    await refreshCurrentProblem();
  } catch (error) {
    setMessage(`Pause error: ${error.message}`);
  } finally {
    state.controlInFlight = false;
    updateContinueButtonState();
    updatePauseButtonState();
    syncAutoRunWithProblemStatus();
  }
}

async function runOnce(trigger = "auto") {
  if (!state.currentProblemId || state.controlInFlight || state.tickInFlight || state.pauseHold) {
    return;
  }
  state.tickInFlight = true;
  updateContinueButtonState();
  try {
    const payload = await runTickForProblem(state.currentProblemId, trigger);
    setMessage(`Execution status: ${payload.status}`);
    await refreshCurrentProblem();
  } catch (error) {
    const code = error?.body?.error?.code;
    if (code === "problem_paused_use_continue") {
      state.pauseHold = true;
      stopAutoRun();
      setMessage("Problem is paused. Use Continue to resume.");
    } else {
      setMessage(`Run error: ${error.message}`);
    }
  } finally {
    state.tickInFlight = false;
    updateContinueButtonState();
    updatePauseButtonState();
  }
}

function startAutoRun(initialTrigger = "auto", runImmediately = false) {
  if (!state.currentProblemId) {
    stopAutoRun();
    return;
  }
  if (state.autoRunTimer) {
    return;
  }
  setAutoRunBadge("running");
  if (runImmediately) {
    void runOnce(initialTrigger);
  }
  state.autoRunTimer = setInterval(() => void runOnce("auto"), 1300);
}

function stopAutoRun() {
  if (state.autoRunTimer) {
    clearInterval(state.autoRunTimer);
    state.autoRunTimer = null;
  }
  setAutoRunBadge("idle");
}

async function deleteCurrentProblem() {
  if (!state.currentProblemId) {
    return;
  }
  const confirmed = window.confirm(`Delete problem ${state.currentProblemId} and its artifacts?`);
  if (!confirmed) {
    return;
  }
  const deletedId = state.currentProblemId;
  const payload = await api(`/v1/debug/problems/${deletedId}`, { method: "DELETE" });
  stopAutoRun();
  stopLiveRefresh();
  closeEventStream();
  state.currentProblemId = null;
  state.selectedRootTrackId = "all";
  await refreshProblemList();
  await refreshCurrentProblem();
  renderAll();
  setMessage(`Deleted problem ${deletedId}.\nrows=${JSON.stringify(payload.deleted_rows)}`);
}

async function resetLocalData() {
  const confirmed = window.confirm("Reset ALL local runs and artifacts? This is destructive.");
  if (!confirmed) {
    return;
  }
  stopAutoRun();
  stopLiveRefresh();
  closeEventStream();
  setMessage("Stopping active runs and deleting local data...");
  const payload = await api("/v1/debug/reset-local", { method: "POST" });
  state.currentProblemId = null;
  state.snapshot = null;
  state.progress = null;
  state.costSummary = null;
  state.llmUsage = null;
  state.problemInput = null;
  state.artifacts = [];
  state.requestArtifacts = [];
  state.requestLog = [];
  state.selectedTreeNodeId = null;
  state.selectedTreeDetailTab = "proof";
  state.selectedRootTrackId = "all";
  state.leanFileCache = {};
  state.selectedRequestEntryId = null;
  state.selectedRequestArtifactKey = null;
  state.requestInspector = null;
  state.requestArtifactContentCache = {};
  state.selectedArtifactKey = null;
  await refreshProblemList();
  renderAll();
  setMessage(`Reset complete.\nrows=${JSON.stringify(payload.deleted_rows)}\nartifacts=${JSON.stringify(payload.deleted_artifacts)}`);
}

async function downloadCurrentProblemLogs() {
  if (!state.currentProblemId) return;
  setMessage("Preparing log archive...");
  try {
    const url = `/v1/debug/problems/${state.currentProblemId}/download-logs`;
    const resp = await fetch(url);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      setMessage(`Download failed: ${err?.detail?.message ?? resp.statusText}`);
      return;
    }
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${state.currentProblemId}_logs.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    setMessage("Log archive downloaded.");
  } catch (e) {
    setMessage(`Download error: ${e.message}`);
  }
}

function installTabListeners() {
  document.querySelectorAll("#tabs-nav .tab").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  });
}

function installControlListeners() {
  byId("problem-select").addEventListener("change", async () => {
    const selected = byId("problem-select").value;
    try {
      await loadProblem(selected, "problem");
      setMessage(selected ? `Loaded ${selected}` : "No problem selected.");
    } catch (error) {
      setMessage(`Load error: ${error.message}`);
    }
  });
  byId("open-problem").addEventListener("click", async () => {
    const selected = (byId("problem-select").value || "").trim();
    if (!selected) {
      return;
    }
    try {
      await loadProblem(selected, "problem");
      setMessage(`Loaded ${selected}`);
    } catch (error) {
      setMessage(`Load error: ${error.message}`);
    }
  });
  byId("tree-root-track").addEventListener("change", (event) => {
    state.selectedRootTrackId = event.target.value || "all";
    state.selectedTreeNodeId = null;
    renderSelectedTreeNodeDetail();
    renderTreeExplorer();
  });
  byId("tree-detail-proof-tab").addEventListener("click", () => {
    state.selectedTreeDetailTab = "proof";
    renderSelectedTreeNodeDetail();
  });
  byId("tree-detail-status-tab").addEventListener("click", () => {
    state.selectedTreeDetailTab = "status";
    renderSelectedTreeNodeDetail();
  });
  byId("tree-detail-lean-tab").addEventListener("click", () => {
    state.selectedTreeDetailTab = "lean";
    renderSelectedTreeNodeDetail();
  });

  byId("continue-problem").addEventListener("click", () => void continueCurrentProblem());
  byId("pause-problem").addEventListener("click", () => void pauseCurrentProblem());
  byId("download-logs").addEventListener("click", () => void downloadCurrentProblemLogs());
  byId("delete-problem").addEventListener("click", () => void deleteCurrentProblem());
  byId("reset-local").addEventListener("click", () => void resetLocalData());
  byId("new-problem").addEventListener("click", () => {
    showSubmitPage();
    setMessage("New problem submission page.");
  });
}

function installLlmFieldListeners() {
  AGENT_KEYS.forEach((agentKey) => {
    byId(`form-${agentKey}-model`).addEventListener("change", () => {
      enforceMiniReasoningConstraint(agentKey);
    });
  });
}

function installSubmitTabListeners() {
  byId("sync-form-to-json").addEventListener("click", () => {
    try {
      syncFormToJsonEditor();
      setMessage("JSON editor updated from guided form.");
    } catch (error) {
      setMessage(`Form sync error: ${error.message}`);
    }
  });

  byId("sync-json-to-form").addEventListener("click", () => {
    try {
      syncJsonToForm();
      setMessage("Guided form updated from JSON editor.");
    } catch (error) {
      setMessage(`JSON parse error: ${error.message}`);
    }
  });

  byId("load-template").addEventListener("click", async () => {
    try {
      await loadTemplate();
      setMessage("Problem create template loaded.");
    } catch (error) {
      setMessage(`Template load error: ${error.message}`);
    }
  });

  byId("submit-request").addEventListener("click", async () => {
    try {
      const response = await submitRequestFromEditor();
      activateTab("overview");
      await refreshCurrentProblem();
      setMessage(`Created problem ${response.problem_id}. Execution started; live polling continuing.`);
    } catch (error) {
      setMessage(`Submit error: ${error.message}`);
    }
  });
}

function installRequestTabListeners() {
  byId("request-source-filter").addEventListener("change", async () => {
    state.selectedRequestEntryId = null;
    state.selectedRequestArtifactKey = null;
    state.requestInspector = null;
    byId("request-log-detail").textContent = "Select a request entry.";
    try {
      await fetchRequestLog();
      renderRequestLog();
      setMessage("Request source filter applied.");
    } catch (error) {
      setMessage(`Request filter error: ${error.message}`);
    }
  });

  byId("request-inspector-view").addEventListener("change", () => {
    if (!state.requestInspector) {
      return;
    }
    state.requestInspector.selectedView = byId("request-inspector-view").value;
    const view = state.requestInspector.selectedView;
    const options = state.requestInspector.related[view] || [];
    if (!options.includes(state.requestInspector.selectedKeyByView[view])) {
      state.requestInspector.selectedKeyByView[view] = options[0] || null;
    }
    renderRequestInspector();
    void renderRequestInspectorDetail();
  });

  byId("request-inspector-artifact").addEventListener("change", () => {
    if (!state.requestInspector) {
      return;
    }
    const view = state.requestInspector.selectedView;
    state.requestInspector.selectedKeyByView[view] = byId("request-inspector-artifact").value || null;
    renderRequestInspector();
    void renderRequestInspectorDetail();
  });

}

function installArtifactTabListeners() {
  byId("artifact-search").addEventListener("input", renderArtifacts);
}

function installEventsTabListeners() {
  byId("load-more-events").addEventListener("click", async () => {
    state.eventsLimit += 250;
    try {
      await fetchSnapshot();
      renderEvents();
      setMessage(`Loaded more events. events_limit=${state.eventsLimit}`);
    } catch (error) {
      setMessage(`Load more events error: ${error.message}`);
    }
  });
  byId("event-stage-filter").addEventListener("input", renderEvents);
  byId("event-text-filter").addEventListener("input", renderEvents);
}

async function bootstrap() {
  const initialPage = pageFromUrl();
  installCopyButtons();
  installTabListeners();
  installControlListeners();
  installLlmFieldListeners();
  installFirstAttemptToggleListeners();
  installSubmitTabListeners();
  installRequestTabListeners();
  installArtifactTabListeners();
  installEventsTabListeners();
  activateTab("overview");

  try {
    await loadTemplate();
    await refreshProblemList();
    const selected = byId("problem-select").value;
    if (selected) {
      await loadProblem(selected, initialPage);
    } else {
      activatePage("submit");
      await refreshCurrentProblem();
      setRefreshBadge();
    }
    setMessage("Debug console ready.");
  } catch (error) {
    setMessage(`Bootstrap error: ${error.message}`);
  }

  document.addEventListener("visibilitychange", () => {
    stopLiveRefresh();
    startLiveRefresh();
  });
}

window.addEventListener("beforeunload", () => {
  closeEventStream();
  stopAutoRun();
  stopLiveRefresh();
});

void bootstrap();
