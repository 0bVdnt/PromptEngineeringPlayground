"use strict";

/* PromptLab frontend.
   Deliberately dependency-free. Results live only in this page's memory:
   no localStorage, sessionStorage, cookies or export history. */

const state = {
  config: null,
  health: null,
  usage: null,
  questions: [],
  selectedId: null,
  mode: "preset",
  customText: "",
  estimate: null,
  estimateSeq: 0,
  estimateTimer: null,
  result: null,
  running: false,
  startedAt: 0,
  elapsedTimer: null,
  bootError: null,
};

const $ = (id) => document.getElementById(id);

const els = {
  modelChip: $("modelChip"),
  providerStatus: $("providerStatus"),
  statusDot: $("statusDot"),
  statusText: $("statusText"),
  quotaText: $("quotaText"),
  usageButton: $("usageButton"),
  healthBanner: $("healthBanner"),
  healthTitle: $("healthTitle"),
  healthMessage: $("healthMessage"),
  healthHint: $("healthHint"),
  retryHealth: $("retryHealth"),
  demoBanner: $("demoBanner"),
  budgetRing: $("budgetRing"),
  budgetCalls: $("budgetCalls"),
  budgetHeadline: $("budgetHeadline"),
  budgetTokensBar: $("budgetTokensBar"),
  budgetTokens: $("budgetTokens"),
  budgetCostBar: $("budgetCostBar"),
  budgetCost: $("budgetCost"),
  budgetNote: $("budgetNote"),
  budgetDetails: $("budgetDetails"),
  questionList: $("questionList"),
  questionCount: $("questionCount"),
  customQuestion: $("customQuestion"),
  customCount: $("customCount"),
  clearCustom: $("clearCustom"),
  methodStack: $("methodStack"),
  questionSummary: $("questionSummary"),
  questionSummaryMeta: $("questionSummaryMeta"),
  currentCard: $("currentCard"),
  runBar: $("runBar"),
  runKicker: $("runKicker"),
  runSummary: $("runSummary"),
  runEstimate: $("runEstimate"),
  runProgress: $("runProgress"),
  runButton: $("runButton"),
  runButtonLabel: $("runButtonLabel"),
  runSpinner: $("runSpinner"),
  runArrow: $("runArrow"),
  results: $("results"),
  resultsMeta: $("resultsMeta"),
  clearResults: $("clearResults"),
  qualitativeNote: $("qualitativeNote"),
  usageDialog: $("usageDialog"),
  usageBody: $("usageBody"),
  closeUsage: $("closeUsage"),
  resetUsage: $("resetUsage"),
  toasts: $("toasts"),
};

document.addEventListener("DOMContentLoaded", init);

async function init() {
  bindEvents();
  renderBootState();

  const healthPromise = api("/api/health").catch((error) => ({
    ok: false,
    state: "offline",
    message: error.message,
    hint: null,
    models: [],
  }));

  try {
    const config = await api("/api/config");
    state.config = config;
    state.usage = config.usage;
    state.questions = config.questions || [];
    state.selectedId = state.questions[0]?.id || null;
    state.mode = "preset";
    renderStatic();
    renderAll();
    scheduleEstimate(0);

    state.health = await healthPromise;
    renderHealth();
    renderRunBar();

    if (config.demo && new URLSearchParams(window.location.search).get("demo") === "1") {
      window.setTimeout(runComparison, 500);
    }
  } catch (error) {
    state.bootError = error;
    renderBootError(error);
  }

  window.setInterval(() => {
    if (state.health && !state.health.ok && !state.running) {
      refreshHealth();
    }
  }, 45000);
}

function bindEvents() {
  els.runButton.addEventListener("click", runComparison);
  els.clearResults.addEventListener("click", clearResults);
  els.usageButton.addEventListener("click", openUsage);
  els.budgetDetails.addEventListener("click", openUsage);
  els.closeUsage.addEventListener("click", () => els.usageDialog.close());
  els.resetUsage.addEventListener("click", resetUsage);
  els.providerStatus.addEventListener("click", refreshHealth);
  els.retryHealth.addEventListener("click", refreshHealth);
  els.clearCustom.addEventListener("click", clearCustomQuestion);

  els.customQuestion.addEventListener("input", (event) => {
    if (state.running) return;
    state.mode = "custom";
    state.customText = event.target.value;
    updateQuestionSelection();
    els.clearCustom.hidden = state.customText.length === 0;
    els.customCount.textContent = `${state.customText.length} / 2000`;
    renderCurrentQuestion();
    scheduleEstimate(320);
  });

  els.questionList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-qid]");
    if (!button || state.running) return;
    selectQuestion(button.dataset.qid);
  });

  els.results.addEventListener("click", async (event) => {
    const copyButton = event.target.closest("[data-copy-method]");
    if (copyButton) {
      await copyResponse(copyButton.dataset.copyMethod, copyButton);
      return;
    }
    const retryButton = event.target.closest("[data-action='retry']");
    if (retryButton && !state.running) {
      runComparison();
    }
  });

  els.usageDialog.addEventListener("click", (event) => {
    if (event.target === els.usageDialog) {
      els.usageDialog.close();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && els.usageDialog.open) {
      els.usageDialog.close();
    }
  });
}

/* ------------------------------------------------------------------ */
/* API                                                                 */
/* ------------------------------------------------------------------ */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const error = new Error(data?.message || `Request failed (${response.status}).`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

/* ------------------------------------------------------------------ */
/* Boot / static rendering                                             */
/* ------------------------------------------------------------------ */

function renderBootState() {
  els.questionSummary.textContent = "Loading the question bank…";
  els.questionSummaryMeta.textContent = "Starting";
  els.runSummary.textContent = "Three prompts, one question";
  els.runEstimate.textContent = "Connecting to the local server…";
  els.results.innerHTML = "";
  els.statusText.textContent = "Connecting…";
}

function renderBootError(error) {
  els.statusDot.className = "status-dot error";
  els.statusText.textContent = "Server offline";
  els.runKicker.textContent = "Cannot start";
  els.runSummary.textContent = "The playground API is unavailable";
  els.runEstimate.textContent = error.message;
  els.runButton.disabled = true;
  els.questionSummary.textContent = "Could not load the question bank.";
  els.questionSummaryMeta.textContent = "Offline";
  els.results.innerHTML = `
    <div class="empty-state">
      <h3>Could not reach the PromptLab server</h3>
      <p>${escapeHTML(error.message)}</p>
      <p style="margin-top:14px">
        <button class="ghost" type="button" data-action="retry">Reload the page</button>
      </p>
    </div>`;
  els.results.querySelector("[data-action='retry']")?.addEventListener("click", () => window.location.reload());
}

function renderStatic() {
  const config = state.config;
  if (!config) return;
  els.modelChip.textContent = config.provider.model;
  els.modelChip.title = `${config.provider.label} · configured on the server`;
  els.questionCount.textContent = `${config.questions.length} in bank`;
  els.methodStack.innerHTML = config.strategies
    .map(
      (strategy, index) => `
        <div class="method-card s-${escapeHTML(strategy.id)}">
          <div class="method-card-head">
            <i></i>
            <strong>${escapeHTML(strategy.label)}</strong>
            <span>0${index + 1}</span>
          </div>
          <p>${escapeHTML(strategy.tagline)}</p>
        </div>`
    )
    .join("");
  els.budgetNote.innerHTML = `
    <span class="shield" aria-hidden="true"></span>
    ${config.demo ? "Demo mode makes no model calls" : "Sequential calls · paced · output capped"}`;
}

function renderAll() {
  renderHeader();
  renderBudget();
  renderQuestions();
  renderCurrentQuestion();
  renderRunBar();
  renderResults();
  renderHealth();
}

function renderHeader() {
  if (state.config) {
    els.modelChip.textContent = state.config.provider.model;
  }
}

/* ------------------------------------------------------------------ */
/* Provider health                                                     */
/* ------------------------------------------------------------------ */

async function refreshHealth() {
  if (!state.config) return;
  els.statusText.textContent = "Checking…";
  els.statusDot.className = "status-dot";
  try {
    state.health = await api("/api/health");
  } catch (error) {
    state.health = {
      ok: false,
      state: "offline",
      message: error.message,
      hint: null,
      models: [],
    };
  }
  renderHealth();
  renderRunBar();
}

function renderHealth() {
  const health = state.health;
  const demo = Boolean(state.config?.demo);
  const provider = state.config?.provider;

  if (!health) {
    els.statusDot.className = "status-dot";
    els.statusText.textContent = "Checking…";
    return;
  }

  if (health.state === "ready") {
    els.statusDot.className = "status-dot ready";
    els.statusText.textContent = `${provider?.label || "Provider"} ready`;
  } else if (health.state === "demo") {
    els.statusDot.className = "status-dot demo";
    els.statusText.textContent = "Demo mode";
  } else if (health.state === "model_missing") {
    els.statusDot.className = "status-dot warn";
    els.statusText.textContent = "Model missing";
  } else if (health.state === "misconfigured") {
    els.statusDot.className = "status-dot warn";
    els.statusText.textContent = "Config needed";
  } else {
    els.statusDot.className = "status-dot error";
    els.statusText.textContent = "Provider offline";
  }

  els.demoBanner.hidden = !demo;

  if (health.ok || demo) {
    els.healthBanner.hidden = true;
    return;
  }

  els.healthBanner.hidden = false;
  const titles = {
    offline: "Provider is offline",
    model_missing: "Model is missing",
    misconfigured: "Provider needs configuration",
  };
  els.healthTitle.textContent = titles[health.state] || "Provider unavailable";
  els.healthMessage.textContent = health.message || "The configured provider could not be reached.";
  if (health.hint) {
    els.healthHint.hidden = false;
    els.healthHint.textContent = health.hint;
  } else {
    els.healthHint.hidden = true;
  }
}

/* ------------------------------------------------------------------ */
/* Budget                                                              */
/* ------------------------------------------------------------------ */

function renderBudget() {
  if (!state.config || !state.usage) return;
  const usage = state.usage;
  const limits = usage.limits;
  const pricing = usage.pricing;
  const requestsPct = percent(usage.requests, limits.maxRequestsPerDay);
  const tokensPct = percent(usage.tokens, limits.maxTokensPerDay);
  const costPct = limits.maxCostPerDay > 0 ? percent(usage.cost, limits.maxCostPerDay) : 0;
  const ringColor =
    requestsPct >= 85 ? "var(--red)" : requestsPct >= 65 ? "var(--amber)" : "var(--accent)";

  els.budgetRing.style.setProperty("--p", requestsPct.toFixed(1));
  els.budgetRing.style.setProperty("--ring-color", ringColor);
  els.budgetCalls.textContent = fmtInt(usage.remaining.requests);
  els.budgetHeadline.innerHTML = state.config.demo
    ? "<b>Demo mode</b> · no calls or quota consumed"
    : `<b>${fmtInt(usage.requests)} of ${fmtInt(limits.maxRequestsPerDay)}</b> calls used today`;
  els.budgetTokensBar.style.width = `${Math.min(100, tokensPct)}%`;
  els.budgetTokens.textContent = `${fmtCompact(usage.tokens)} / ${fmtCompact(limits.maxTokensPerDay)}`;
  const pricesConfigured =
    Number(pricing.inputPerMillion || 0) > 0 || Number(pricing.outputPerMillion || 0) > 0;
  if (!pricesConfigured && !state.config.provider.isLocal) {
    els.budgetCostBar.style.width = "0%";
    els.budgetCost.textContent = "not priced";
  } else {
    els.budgetCostBar.style.width = `${Math.min(100, costPct)}%`;
    els.budgetCost.textContent = `${money(usage.cost, 4)} / ${money(limits.maxCostPerDay, 2)}`;
  }
  els.quotaText.textContent = state.config.demo
    ? "Demo · no quota"
    : `${fmtInt(usage.remaining.requests)} / ${fmtInt(limits.maxRequestsPerDay)} calls left`;

  if (!state.config.demo) {
    els.budgetNote.innerHTML = `
      <span class="shield" aria-hidden="true"></span>
      ${fmtInt(limits.maxOutputTokens)} token cap · ${(limits.minIntervalMs / 1000).toFixed(1)}s between calls
      · ${escapeHTML(pricing.currency)} ${Number(limits.maxCostPerDay).toFixed(2)}/day`;
  }
}

function percent(value, max) {
  if (!max || max <= 0) return 0;
  return Math.max(0, Math.min(100, (Number(value || 0) / Number(max)) * 100));
}

/* ------------------------------------------------------------------ */
/* Questions                                                           */
/* ------------------------------------------------------------------ */

function renderQuestions() {
  if (!state.config) return;
  els.questionList.innerHTML = state.questions
    .map((question) => {
      const selected = state.mode === "preset" && question.id === state.selectedId;
      return `
        <button
          class="question-item ${selected ? "is-selected" : ""}"
          type="button"
          data-qid="${escapeHTML(question.id)}"
          aria-pressed="${selected}"
        >
          <span class="q-bullet" aria-hidden="true"></span>
          <span class="q-content">
            <span class="q-top">
              <span class="cat cat-${escapeHTML(question.color || "slate")}">${escapeHTML(question.category)}</span>
              <span class="q-id">${escapeHTML(question.id)}</span>
            </span>
            <span class="q-title">${escapeHTML(question.title)}</span>
            <span class="q-text">${escapeHTML(question.text)}</span>
          </span>
        </button>`;
    })
    .join("");

  els.customQuestion.value = state.customText;
  els.customCount.textContent = `${state.customText.length} / 2000`;
  els.clearCustom.hidden = state.customText.length === 0;
}

function updateQuestionSelection() {
  els.questionList.querySelectorAll("[data-qid]").forEach((button) => {
    const selected = state.mode === "preset" && button.dataset.qid === state.selectedId;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

function selectQuestion(id) {
  if (state.running) return;
  state.mode = "preset";
  state.selectedId = id;
  state.customText = "";
  els.customQuestion.value = "";
  renderQuestions();
  renderCurrentQuestion();
  scheduleEstimate(0);
}

function clearCustomQuestion() {
  if (state.running) return;
  state.customText = "";
  state.mode = "preset";
  if (!state.selectedId) {
    state.selectedId = state.questions[0]?.id || null;
  }
  els.customQuestion.value = "";
  updateQuestionSelection();
  els.clearCustom.hidden = true;
  els.customCount.textContent = "0 / 2000";
  renderCurrentQuestion();
  scheduleEstimate(0);
}

function currentQuestion() {
  if (state.mode === "custom") {
    return state.customText.trim();
  }
  return state.questions.find((question) => question.id === state.selectedId)?.text || "";
}

function currentQuestionId() {
  return state.mode === "custom" ? "custom" : state.selectedId || "custom";
}

function renderCurrentQuestion() {
  const question = currentQuestion();
  if (!question) {
    els.questionSummary.textContent = "Choose a question to begin.";
    els.questionSummaryMeta.textContent = "Not selected";
    return;
  }
  els.questionSummary.textContent = question;
  if (state.mode === "custom") {
    els.questionSummaryMeta.textContent = "Custom question";
  } else {
    const preset = state.questions.find((item) => item.id === state.selectedId);
    els.questionSummaryMeta.textContent = preset
      ? `${preset.id} · ${preset.category}`
      : "Question";
  }
}

/* ------------------------------------------------------------------ */
/* Estimate and run bar                                                */
/* ------------------------------------------------------------------ */

function scheduleEstimate(delay = 250) {
  window.clearTimeout(state.estimateTimer);
  state.estimateTimer = window.setTimeout(refreshEstimate, delay);
}

async function refreshEstimate() {
  if (state.running || !state.config) return;
  const question = currentQuestion();
  if (!question) {
    state.estimate = null;
    renderRunBar();
    return;
  }

  const sequence = ++state.estimateSeq;
  state.estimate = null;
  renderRunBar();

  try {
    const estimate = await api("/api/estimate", {
      method: "POST",
      body: JSON.stringify({
        question,
        questionId: currentQuestionId(),
      }),
    });
    if (sequence !== state.estimateSeq) return;
    state.estimate = estimate;
    if (estimate.remaining) {
      state.usage = {
        ...state.usage,
        remaining: estimate.remaining,
      };
    }
  } catch (error) {
    if (sequence !== state.estimateSeq) return;
    state.estimate = {
      error: error.message,
      fits: false,
      blockers: [{ code: "estimate", message: error.message }],
    };
  }
  renderRunBar();
}

function healthBlocksRun() {
  return Boolean(state.health && !state.health.ok && !state.config?.demo);
}

function canRun() {
  return Boolean(
    state.config &&
      currentQuestion() &&
      state.estimate &&
      state.estimate.fits &&
      !state.estimate.error &&
      !healthBlocksRun() &&
      !state.running
  );
}

function renderRunBar() {
  const question = currentQuestion();
  const estimate = state.estimate;
  const blockedByHealth = healthBlocksRun();

  els.customQuestion.disabled = state.running;
  els.runBar.classList.toggle("is-running", state.running);
  els.runBar.classList.toggle(
    "is-blocked",
    !state.running && Boolean((estimate && !estimate.fits) || blockedByHealth)
  );
  els.runProgress.hidden = !state.running;
  els.runSpinner.hidden = !state.running;
  els.runArrow.hidden = state.running;

  if (state.running) {
    els.runKicker.textContent = "Comparison in progress";
    els.runSummary.textContent = "Three prompts are running";
    els.runEstimate.textContent = `${elapsedSeconds().toFixed(1)}s elapsed · sequential calls · same model and settings`;
    els.runButtonLabel.textContent = "Comparing…";
    els.runButton.disabled = true;
    els.runButton.title = "A comparison is already running.";
    return;
  }

  if (!question) {
    els.runKicker.textContent = "Ready to compare";
    els.runSummary.textContent = "Three prompts, one question";
    els.runEstimate.textContent = "Choose a question from the bank, or write a custom one.";
    els.runButtonLabel.textContent = "Compare prompts";
    els.runButton.disabled = true;
    els.runButton.title = "Choose a question first.";
    return;
  }

  if (blockedByHealth) {
    els.runKicker.textContent = "Provider unavailable";
    els.runSummary.textContent = "Three prompts, one question";
    els.runEstimate.textContent = state.health?.message || "Check the provider banner above.";
    els.runButtonLabel.textContent = "Compare prompts";
    els.runButton.disabled = true;
    els.runButton.title = state.health?.message || "Provider unavailable.";
    return;
  }

  if (!estimate) {
    els.runKicker.textContent = "Checking budget";
    els.runSummary.textContent = "Three prompts, one question";
    els.runEstimate.textContent = "Estimating the worst-case token use before running…";
    els.runButtonLabel.textContent = "Compare prompts";
    els.runButton.disabled = true;
    els.runButton.title = "Budget estimate is loading.";
    return;
  }

  if (estimate.error) {
    els.runKicker.textContent = "Estimate unavailable";
    els.runSummary.textContent = "Three prompts, one question";
    els.runEstimate.textContent = estimate.error;
    els.runButtonLabel.textContent = "Compare prompts";
    els.runButton.disabled = true;
    els.runButton.title = estimate.error;
    return;
  }

  const calls = estimate.calls || 3;
  const tokens = estimate.tokensCeiling || 0;
  const cost = Number(estimate.costCeiling || 0);
  const priced =
    Number(state.config.pricing?.inputPerMillion || 0) > 0 ||
    Number(state.config.pricing?.outputPerMillion || 0) > 0;
  const costText = priced
    ? ` · worst case ${money(cost, 4)}`
    : state.config.provider.isLocal
      ? " · local spend $0"
      : " · prices not set";
  els.runSummary.textContent = estimate.demo
    ? "Three prompts · no model calls"
    : calls === 3
      ? "Three prompts · three calls"
      : `${calls} prompts · ${calls} calls`;

  if (estimate.demo) {
    els.runKicker.textContent = "Ready for a demo run";
    els.runEstimate.textContent = "No model calls · illustrative answers · no quota used";
    els.runButtonLabel.textContent = "Run demo comparison";
    els.runButton.disabled = false;
    els.runButton.title = "Demo mode does not call a model.";
    return;
  }

  if (!estimate.fits) {
    const blocker = estimate.blockers?.[0]?.message || "This comparison does not fit today's budget.";
    els.runKicker.textContent = "Budget guard";
    els.runEstimate.innerHTML = `<span class="bad-text">${escapeHTML(blocker)}</span>`;
    els.runButtonLabel.textContent = "Budget check";
    els.runButton.disabled = true;
    els.runButton.title = blocker;
    return;
  }

  els.runKicker.textContent = "Ready to compare";
  els.runEstimate.innerHTML =
    `${calls} calls · worst case ${fmtCompact(tokens)} tokens${escapeHTML(costText)} · ` +
    `<span style="color:var(--green)">within today's budget</span>`;
  els.runButtonLabel.textContent = "Compare prompts";
  els.runButton.disabled = !canRun();
  els.runButton.title = "Run the same question through all three prompt styles.";
}

function elapsedSeconds() {
  if (!state.startedAt) return 0;
  return Math.max(0, (performance.now() - state.startedAt) / 1000);
}

/* ------------------------------------------------------------------ */
/* Comparison                                                          */
/* ------------------------------------------------------------------ */

async function runComparison() {
  if (state.running || !state.config) return;
  const question = currentQuestion();
  if (!question) {
    toast("Choose a question first.", "warn");
    return;
  }
  if (state.estimate && !state.estimate.fits) {
    openUsage();
    toast(state.estimate.blockers?.[0]?.message || "The comparison does not fit today's budget.", "warn");
    return;
  }
  if (healthBlocksRun()) {
    toast(state.health?.message || "The provider is unavailable.", "error");
    return;
  }

  state.running = true;
  state.result = null;
  state.startedAt = performance.now();
  els.clearResults.disabled = true;
  renderRunBar();
  renderResults();
  window.clearInterval(state.elapsedTimer);
  state.elapsedTimer = window.setInterval(() => {
    if (!state.running) return;
    renderRunBar();
  }, 250);

  const payload = {
    question,
    questionId: currentQuestionId(),
  };

  try {
    const data = await api("/api/compare", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.result = data;
    if (data.usage) {
      state.usage = data.usage;
    }
    const successes = (data.results || []).filter((item) => item.ok).length;
    if (successes === 3) {
      toast("All three responses returned.", "success");
    } else if (successes > 0) {
      toast(`${successes} of 3 responses returned. The failed panel shows the reason.`, "warn");
      refreshHealth();
    } else {
      toast("No responses returned. Inspect the prompts and provider message.", "error");
      refreshHealth();
    }
  } catch (error) {
    const data = error.data || {};
    if (data.usage) {
      state.usage = data.usage;
    }
    state.estimate = null;
    state.result = {
      fatal: true,
      error: {
        code: data.error || "request_failed",
        message: error.message,
        hint: data.hint || null,
        status: error.status || null,
      },
    };
    toast(error.message, error.status === 429 ? "warn" : "error");
    if (error.status === 429) {
      openUsage();
    }
  } finally {
    state.running = false;
    state.startedAt = 0;
    window.clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
    els.clearResults.disabled = !state.result;
    renderAll();
    scheduleEstimate(0);
    if (state.result?.fatal && state.result.error?.status === 502) {
      refreshHealth();
    }
  }
}

function clearResults() {
  if (state.running) return;
  state.result = null;
  els.clearResults.disabled = true;
  renderResults();
  toast("Results cleared from this tab.", "info");
}

/* ------------------------------------------------------------------ */
/* Results                                                             */
/* ------------------------------------------------------------------ */

function strategyById(id) {
  return state.config?.strategies.find((strategy) => strategy.id === id) || {
    id,
    label: id,
    color: "slate",
  };
}

function renderResults() {
  if (!state.config) return;
  const strategies = state.config.strategies;
  els.clearResults.disabled = state.running || !state.result;
  els.qualitativeNote.hidden = true;

  if (state.running) {
    els.resultsMeta.textContent = "Running three prompts sequentially. The first call may load the model.";
    els.results.innerHTML = `<div class="comparison-grid">${strategies
      .map((strategy) => skeletonCard(strategy))
      .join("")}</div>`;
    return;
  }

  if (!state.result) {
    els.resultsMeta.textContent = "Results stay in this browser tab. Refresh to clear them.";
    els.results.innerHTML = emptyState(strategies);
    return;
  }

  if (state.result.fatal) {
    const error = state.result.error || {};
    els.resultsMeta.textContent = "The comparison could not start.";
    els.results.innerHTML = `
      <div class="empty-state">
        <h3>Comparison could not start</h3>
        <p>${escapeHTML(error.message || "Unknown error.")}</p>
        ${error.hint ? `<p class="error-hint">${escapeHTML(error.hint)}</p>` : ""}
        <p style="margin-top:16px">
          <button class="ghost" type="button" data-action="retry">Try again</button>
        </p>
      </div>`;
    const retryButton = els.results.querySelector("[data-action='retry']");
    retryButton?.addEventListener("click", async () => {
      await refreshHealth();
      runComparison();
    });
    return;
  }

  const data = state.result;
  const results = Array.isArray(data.results) ? data.results : [];
  const successCount = results.filter((result) => result.ok).length;
  const totalTokens = results.reduce(
    (sum, result) => sum + Number(result.totalTokens || 0),
    0
  );
  const question = data.question || {};
  const model = data.generation?.model || state.config.provider.model;
  const temperature = data.generation?.temperature ?? state.config.defaults.temperature;
  const maxTokens = data.generation?.maxOutputTokens ?? state.config.defaults.maxOutputTokens;

  els.resultsMeta.textContent = `${question.id || "Q"} · ${question.category || "Custom"} · ${model} · temp ${temperature} · ${maxTokens} token cap${data.provider?.isLocal ? " · local" : ""}`;
  els.qualitativeNote.hidden = false;

  const summaryClass = successCount === 3 ? "" : successCount > 0 ? "warn" : "bad";
  const summary = `
    <div class="result-summary">
      <span class="summary-pill ${summaryClass}"><i></i><strong>${successCount} / ${results.length || 3}</strong>&nbsp;succeeded</span>
      ${
        data.demo
          ? `<span class="summary-pill">Demo · simulated stats</span>`
          : `<span class="summary-pill">${formatLatency(data.elapsedMs)} wall time</span>
             <span class="summary-pill">${fmtInt(totalTokens)} tokens measured</span>`
      }
      ${question.custom ? `<span class="summary-pill">Custom question</span>` : ""}
    </div>`;

  els.results.innerHTML =
    summary +
    `<div class="comparison-grid">${results
      .map((result) => resultCard(result))
      .join("")}</div>`;
}

function skeletonCard(strategy) {
  return `
    <article class="result-card s-${escapeHTML(strategy.id)}">
      <header class="result-head">
        <div class="result-method"><i></i><h3>${escapeHTML(strategy.label)}</h3></div>
        <span class="status status-running">Running</span>
      </header>
      <div class="result-body is-pending">
        <div class="loading-stack">
          <div class="skeleton-line"></div>
          <div class="skeleton-line"></div>
          <div class="skeleton-line"></div>
          <div class="skeleton-line"></div>
          <div class="skeleton-line"></div>
        </div>
        <p class="loading-caption">Waiting for the model…</p>
      </div>
    </article>`;
}

function emptyState(strategies) {
  return `
    <div class="empty-state">
      <div class="empty-panels">
        ${strategies
          .map(
            (strategy) => `
            <div class="empty-panel s-${escapeHTML(strategy.id)}">
              <div class="empty-panel-head"><i></i>${escapeHTML(strategy.label)}</div>
              <div class="skeleton-line"></div>
              <div class="skeleton-line"></div>
              <div class="skeleton-line"></div>
              <div class="skeleton-line"></div>
            </div>`
          )
          .join("")}
      </div>
      <h3>Three prompts. One question. Side by side.</h3>
      <p>
        Direct is the baseline. Structured adds context, constraints and a response contract.
        Step-by-step asks for a concise explanation of the working before the final answer.
        Every result includes the exact prompt that was sent.
      </p>
      <div class="empty-points">
        <span>Same model</span>
        <span>Same temperature</span>
        <span>Same output cap</span>
        <span>Prompts inspectable</span>
      </div>
    </div>`;
}

function resultCard(result) {
  const strategy = strategyById(result.method);
  const status = result.demo
    ? `<span class="status status-demo">Demo</span>`
    : result.ok
      ? `<span class="status status-done">Done</span>`
      : `<span class="status status-error">Error</span>`;

  const body = result.ok
    ? `${result.demo ? '<div class="demo-flag">◆ Illustrative demo answer — no model was called</div>' : ""}
       <div class="answer">${renderMarkdown(result.response || "")}</div>`
    : errorBody(result.error);

  const stats = result.ok ? resultStats(result) : `<span class="stat">No tokens used</span>`;

  return `
    <article class="result-card s-${escapeHTML(strategy.id)}">
      <header class="result-head">
        <div class="result-method"><i></i><h3>${escapeHTML(strategy.label)}</h3></div>
        ${status}
      </header>
      <div class="result-body">${body}</div>
      <footer class="result-foot">
        ${stats}
        ${
          result.ok
            ? `<div class="result-actions">
                 <button class="copy-btn" type="button" data-copy-method="${escapeHTML(result.method)}">Copy</button>
               </div>`
            : ""
        }
      </footer>
      ${promptDetails(result)}
    </article>`;
}

function resultStats(result) {
  const stats = [];
  const latency = result.demo
    ? `${formatLatency(result.latencyMs)} sim`
    : formatLatency(result.latencyMs);
  stats.push(`<span class="stat" title="${result.demo ? "Simulated latency for the demo card" : "Wall-clock latency measured by the server"}">${latency}</span>`);
  stats.push(
    `<span class="stat" title="${result.demo ? "Simulated token count" : `Prompt ${fmtInt(result.promptTokens)} + completion ${fmtInt(
      result.completionTokens
    )}${result.estimatedTokens ? " (estimated)" : ""}`}">${fmtInt(result.totalTokens)} tok</span>`
  );
  if (result.demo) {
    stats.push(`<span class="stat">no quota</span>`);
  } else if (Number(result.cost) > 0) {
    stats.push(`<span class="stat">${money(result.cost, 4)}</span>`);
  } else if (state.config.provider.isLocal) {
    stats.push(`<span class="stat">local · $0</span>`);
  } else {
    stats.push(`<span class="stat">spend not priced</span>`);
  }
  if (result.truncated) {
    stats.push(`<span class="stat truncated" title="The provider stopped at the output-token cap">truncated</span>`);
  }
  return stats.join("");
}

function errorBody(error) {
  if (!error) {
    return `<div class="error-box"><strong>Request failed</strong><p>No error details were returned.</p></div>`;
  }
  return `
    <div class="error-box">
      <strong>This prompt did not return a response</strong>
      <p>${escapeHTML(error.message || "Unknown error.")}</p>
      ${error.hint ? `<p class="error-hint">${escapeHTML(error.hint)}</p>` : ""}
      <code>${escapeHTML(error.code || "error")}</code>
    </div>`;
}

function promptDetails(result) {
  if (!result.prompt) return "";
  return `
    <details class="prompt-details">
      <summary>Inspect exact prompt</summary>
      <div class="prompt-content">
        <div class="prompt-block">
          <span>System</span>
          <pre>${escapeHTML(result.prompt.system || "(no system prompt)")}</pre>
        </div>
        <div class="prompt-block">
          <span>User</span>
          <pre>${escapeHTML(result.prompt.user || "")}</pre>
        </div>
      </div>
    </details>`;
}

/* ------------------------------------------------------------------ */
/* Copy                                                                */
/* ------------------------------------------------------------------ */

async function copyResponse(method, button) {
  const result = state.result?.results?.find((item) => item.method === method);
  if (!result?.ok) return;
  const copied = await copyText(result.response || "");
  if (copied) {
    const original = button.textContent;
    button.textContent = "Copied";
    button.disabled = true;
    window.setTimeout(() => {
      button.textContent = original;
      button.disabled = false;
    }, 1400);
  } else {
    toast("Could not copy to the clipboard.", "error");
  }
}

async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through to the legacy path.
  }
  try {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const success = document.execCommand("copy");
    textarea.remove();
    return success;
  } catch {
    return false;
  }
}

/* ------------------------------------------------------------------ */
/* Usage dialog                                                        */
/* ------------------------------------------------------------------ */

function openUsage() {
  if (!state.config || !state.usage) return;
  renderUsage();
  if (!els.usageDialog.open) {
    els.usageDialog.showModal();
  }
}

async function resetUsage() {
  if (!window.confirm("Reset today's anonymous usage counters? This does not change any provider-side quota.")) {
    return;
  }
  try {
    const data = await api("/api/usage/reset", {
      method: "POST",
      body: JSON.stringify({ confirm: "reset" }),
    });
    state.usage = data.usage;
    renderBudget();
    renderUsage();
    renderRunBar();
    scheduleEstimate(0);
    toast("Usage counters reset.", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderUsage() {
  if (!state.config || !state.usage) return;
  const usage = state.usage;
  const limits = usage.limits;
  const pricing = usage.pricing;
  const requestsPct = percent(usage.requests, limits.maxRequestsPerDay);
  const tokensPct = percent(usage.tokens, limits.maxTokensPerDay);
  const costPct = limits.maxCostPerDay > 0 ? percent(usage.cost, limits.maxCostPerDay) : 0;
  const ringColor =
    requestsPct >= 85 ? "var(--red)" : requestsPct >= 65 ? "var(--amber)" : "var(--accent)";

  const rows = (state.config.strategies || [])
    .map((strategy) => {
      const bucket = usage.byStrategy?.[strategy.id] || {};
      return `
        <tr>
          <td>${escapeHTML(strategy.label)}</td>
          <td>${fmtInt(bucket.requests || 0)}</td>
          <td>${fmtInt(bucket.promptTokens || 0)}</td>
          <td>${fmtInt(bucket.completionTokens || 0)}</td>
          <td>${money(bucket.cost || 0, 4)}</td>
        </tr>`;
    })
    .join("");

  const pricesConfigured =
    Number(pricing.inputPerMillion || 0) > 0 || Number(pricing.outputPerMillion || 0) > 0;
  const pricingNote = pricesConfigured
    ? ""
    : state.config.provider.isLocal
      ? "<br><strong>Pricing:</strong> prices are set to zero for the local provider, so spend stays at $0.00."
      : "<br><strong>Pricing:</strong> no per-token prices are configured, so the spend meter is inactive. Set <code>PROMPT_INPUT_PRICE_PER_M</code> and <code>PROMPT_OUTPUT_PRICE_PER_M</code> to track estimated spend.";

  els.usageBody.innerHTML = `
    <div class="usage-overview">
      <div class="usage-ring" style="--p:${requestsPct.toFixed(1)};--ring-color:${ringColor}">
        <div class="usage-ring-inner">
          <strong>${fmtInt(usage.remaining.requests)}</strong>
          <span>calls left</span>
        </div>
      </div>
      <div class="usage-metrics">
        <div class="usage-metric">
          <span>Calls used</span>
          <strong>${fmtInt(usage.requests)} / ${fmtInt(limits.maxRequestsPerDay)}</strong>
          <small>${fmtInt(usage.remaining.requests)} remaining</small>
        </div>
        <div class="usage-metric">
          <span>Tokens used</span>
          <strong>${fmtCompact(usage.tokens)}</strong>
          <small>${fmtCompact(limits.maxTokensPerDay)} daily cap</small>
        </div>
        <div class="usage-metric">
          <span>Spend</span>
          <strong>${money(usage.cost, 4)}</strong>
          <small>${money(limits.maxCostPerDay, 2)} daily cap</small>
        </div>
      </div>
    </div>

    <section class="usage-section">
      <h3>Today's guardrails</h3>
      <div class="limit-list">
        ${limitRow("Requests", usage.requests, limits.maxRequestsPerDay, requestsPct, "calls")}
        ${limitRow("Tokens", usage.tokens, limits.maxTokensPerDay, tokensPct, "tokens")}
        ${limitRow("Cost", usage.cost, limits.maxCostPerDay, costPct, "")}
      </div>
    </section>

    <section class="usage-section">
      <h3>By prompt style</h3>
      <table class="usage-table">
        <thead>
          <tr><th>Method</th><th>Calls</th><th>Prompt</th><th>Completion</th><th>Cost</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </section>

    <p class="usage-note">
      <strong>Privacy:</strong> prompt text and responses are never written to disk.
      Only the anonymous counters above are persisted, in a server-side counter file.
      <br>
      <strong>How calls are controlled:</strong> a comparison makes three sequential calls with the same
      model and settings, spaces them at least ${(limits.minIntervalMs / 1000).toFixed(1)}s apart, caps
      completion at ${fmtInt(limits.maxOutputTokens)} tokens, and rejects a run whose worst case would
      cross a daily ceiling.
      ${pricingNote}
    </p>`;

  els.resetUsage.hidden = !state.config.allowReset;
}

function limitRow(label, used, max, pct, suffix) {
  const display =
    label === "Cost"
      ? `${money(used, 4)} / ${money(max, 2)}`
      : `${fmtInt(used)} / ${fmtInt(max)} ${suffix}`;
  return `
    <div class="limit-row">
      <span>${escapeHTML(label)}</span>
      <div class="limit-track"><i style="width:${Math.min(100, pct)}%"></i></div>
      <b>${display}</b>
    </div>`;
}

/* ------------------------------------------------------------------ */
/* Formatting                                                          */
/* ------------------------------------------------------------------ */

function fmtInt(value) {
  return Number(value || 0).toLocaleString("en-US");
}

function fmtCompact(value) {
  const number = Number(value || 0);
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1).replace(/\.0$/, "")}m`;
  if (number >= 10_000) return `${Math.round(number / 1_000)}k`;
  if (number >= 1_000) return `${(number / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(number);
}

function formatLatency(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return "—";
  const number = Number(ms);
  if (number < 1000) return `${Math.round(number)} ms`;
  return `${(number / 1000).toFixed(1)}s`;
}

function money(value, digits = 2) {
  const currency = state.config?.pricing?.currency || state.usage?.pricing?.currency || "USD";
  const number = Number(value || 0);
  return currency === "USD"
    ? `$${number.toFixed(digits)}`
    : `${currency} ${number.toFixed(digits)}`;
}

/* ------------------------------------------------------------------ */
/* Safe, small Markdown renderer                                       */
/* ------------------------------------------------------------------ */

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => {
    switch (character) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      case "'": return "&#39;";
      default: return character;
    }
  });
}

function renderInline(escaped) {
  const codes = [];
  let text = escaped.replace(/`([^`]+)`/g, (_match, code) => {
    const token = `\u0000C${codes.length}\u0000`;
    codes.push(`<code>${code}</code>`);
    return token;
  });
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  text = text.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  text = text.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>'
  );
  text = text.replace(/\u0000C(\d+)\u0000/g, (_match, index) => codes[Number(index)] || "");
  return text;
}

function renderMarkdown(source) {
  const input = String(source || "").replace(/\r\n/g, "\n").trim();
  if (!input) return "";

  const blocks = [];
  const withPlaceholders = input.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, (_match, _lang, code) => {
    const token = `\u0000B${blocks.length}\u0000`;
    blocks.push(`<pre><code>${escapeHTML(code.replace(/\n$/, ""))}</code></pre>`);
    return token;
  });

  const lines = withPlaceholders.split("\n");
  let html = "";
  let paragraph = [];
  let listType = null;
  let listItems = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html += `<p>${renderInline(escapeHTML(paragraph.join(" ")))}</p>`;
    paragraph = [];
  };

  const flushList = () => {
    if (!listType) return;
    html += `<${listType}>${listItems
      .map((item) => `<li>${renderInline(escapeHTML(item))}</li>`)
      .join("")}</${listType}>`;
    listType = null;
    listItems = [];
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();

    if (!line) {
      flushParagraph();
      flushList();
      continue;
    }

    const blockToken = line.match(/^\u0000B(\d+)\u0000$/);
    if (blockToken) {
      flushParagraph();
      flushList();
      html += blocks[Number(blockToken[1])] || "";
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) {
      flushParagraph();
      flushList();
      html += "<hr>";
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = Math.min(4, heading[1].length);
      html += `<h${level}>${renderInline(escapeHTML(heading[2]))}</h${level}>`;
      continue;
    }

    const unordered = line.match(/^[-*+]\s+(.+)$/);
    if (unordered) {
      flushParagraph();
      if (listType !== "ul") {
        flushList();
        listType = "ul";
      }
      listItems.push(unordered[1]);
      continue;
    }

    const ordered = line.match(/^\d+[.)]\s+(.+)$/);
    if (ordered) {
      flushParagraph();
      if (listType !== "ol") {
        flushList();
        listType = "ol";
      }
      listItems.push(ordered[1]);
      continue;
    }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      flushList();
      html += `<blockquote>${renderInline(escapeHTML(quote[1]))}</blockquote>`;
      continue;
    }

    flushList();
    paragraph.push(line);
  }

  flushParagraph();
  flushList();
  return html;
}

/* ------------------------------------------------------------------ */
/* Toasts                                                              */
/* ------------------------------------------------------------------ */

function toast(message, kind = "info", timeout = 4500) {
  const element = document.createElement("div");
  element.className = `toast ${kind}`;
  element.textContent = message;
  els.toasts.appendChild(element);

  while (els.toasts.children.length > 4) {
    els.toasts.firstElementChild?.remove();
  }

  window.setTimeout(() => {
    element.classList.add("leaving");
    window.setTimeout(() => element.remove(), 200);
  }, timeout);
}
