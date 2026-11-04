// Advice progressive enhancement: async POST plus serialized status polling.
// English comments per project convention; user-visible text comes from the
// template data-msg-* attributes (Django i18n) and uses textContent only.
// No paid calls here: the player POSTs once per explicit submit and polls
// the read-only status endpoint with GET. No fake progress is ever shown.
// No Content-Security-Policy is configured in this project (no CSP
// middleware, headers, or meta tags), so this external static script loads
// without inline-script allowances.
(function () {
  "use strict";

  // Serialized polling cadence: first retry after 2s, capped at 5s.
  var POLL_FIRST_MS = 2000;
  var POLL_MAX_MS = 5000;

  var state = { busy: false, generation: 0, pollTimer: 0, statusUrl: "", controller: null };

  function readCookie(name) {
    var prefix = name + "=";
    var parts = document.cookie ? document.cookie.split(";") : [];
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i].trim();
      if (part.indexOf(prefix) === 0) {
        return decodeURIComponent(part.slice(prefix.length));
      }
    }
    return "";
  }

  function setText(el, value) {
    if (el) {
      el.textContent = value;
    }
  }

  function currentLang() {
    return (document.documentElement.getAttribute("lang") || "hu").toLowerCase();
  }

  function message(el, name, fallbackHu, fallbackEn) {
    var value = el.getAttribute("data-msg-" + name);
    if (value) {
      return value;
    }
    return currentLang() === "en" ? fallbackEn : fallbackHu;
  }

  function wantsJson(response) {
    var kind = response.headers.get("Content-Type") || "";
    return kind.indexOf("application/json") !== -1;
  }

  function asPayload(response) {
    if (!wantsJson(response)) {
      return Promise.resolve(null);
    }
    return response.json().then(
      function (payload) {
        return { response: response, payload: payload };
      },
      function () {
        return { response: response, payload: null };
      }
    );
  }

  function showRetry(retryEl, show) {
    if (retryEl) {
      retryEl.hidden = !show;
    }
  }

  function setIdle(statusEl, submitBtn) {
    state.busy = false;
    if (submitBtn) {
      submitBtn.disabled = false;
    }
    statusEl.setAttribute("aria-busy", "false");
  }

  function stopPolling() {
    state.generation += 1;
    if (state.pollTimer) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = 0;
    }
    if (state.controller) {
      try {
        state.controller.abort();
      } catch (ignored) {
        void ignored;
      }
      state.controller = null;
    }
  }

  function unknownOutcome(statusEl, submitBtn, retryEl) {
    // The POST or status read outcome is unknown (network failure, invalid
    // JSON): never mint a new key, never POST again on our own. The user may
    // retry explicitly with the SAME key or resume polling the known URL.
    setIdle(statusEl, submitBtn);
    setText(statusEl, message(statusEl, "unknown",
      "Hálózati hiba: az eredmény ismeretlen. Próbálja újra ugyanazzal a kéréskulccsal.",
      "Network error: the result is unknown. Retry with the same request key."));
    showRetry(retryEl, true);
  }

  function insertResult(resultEl, html) {
    if (resultEl && typeof html === "string" && html) {
      resultEl.innerHTML = html;
    }
  }

  function finishTerminal(statusEl, submitBtn, retryEl, resultEl, payload, okKey) {
    insertResult(resultEl, payload.html);
    // Terminal phase must not stay queued/running: prefer the server phase
    // (done/error), otherwise derive it from the terminal status.
    var phase = payload.phase === "done" || payload.phase === "error"
      ? payload.phase : (okKey === "done" ? "done" : "error");
    statusEl.setAttribute("data-phase", phase);
    if (payload.status) {
      statusEl.setAttribute("data-status", payload.status);
    }
    setIdle(statusEl, submitBtn);
    showRetry(retryEl, false);
    setText(statusEl, message(statusEl, okKey,
      okKey === "done" ? "A tanács elkészült." : "A tanácskérés sikertelen.",
      okKey === "done" ? "The advice is ready." : "The advice request failed."));
  }

  function startPoll(statusEl, retryEl, resultEl, url, token, delay, submitBtn) {
    if (state.controller) {
      try {
        state.controller.abort();
      } catch (ignoredAbort) {
        void ignoredAbort;
      }
    }
    var ctrl = new AbortController();
    state.controller = ctrl;
    fetch(url, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: ctrl.signal,
    }).then(asPayload, function () {
      // Network rejection: keep a null box so the next step can recover.
      return null;
    }).then(function (boxed) {
      // Stale guard: an unmounted region or a newer run owns the UI now.
      if (token !== state.generation || !statusEl.isConnected) {
        return;
      }
      if (!boxed) {
        // Fetch itself failed: leave the UI idle and retryable.
        unknownOutcome(statusEl, submitBtn, retryEl);
        return;
      }
      var payload = boxed.payload;
      if (!payload || (payload.status !== "pending" && payload.status !== "success" && payload.status !== "failed")) {
        unknownOutcome(statusEl, submitBtn, retryEl);
        return;
      }
      if (payload.status === "pending") {
        statusEl.setAttribute("aria-busy", "true");
        // Only server-returned phase drives the label: never synthesized.
        if (payload.phase) {
          statusEl.setAttribute("data-phase", payload.phase);
        }
        setText(statusEl, payload.phase === "running"
          ? message(statusEl, "generating",
            "A tanács generálása folyamatban…", "Generating advice…")
          : message(statusEl, "waiting",
            "Várakozás az eredményre…", "Waiting for the result…"));
        var next = Math.min(delay * 2, POLL_MAX_MS);
        state.pollTimer = window.setTimeout(function () {
          if (token === state.generation) {
            startPoll(statusEl, retryEl, resultEl, url, token, next, submitBtn);
          }
        }, delay);
        return;
      }
      finishTerminal(statusEl, submitBtn, retryEl, resultEl, payload,
        payload.status === "success" ? "done" : "failed");
    }, function (err) {
      if (err && err.name === "AbortError") {
        return;
      }
      if (token !== state.generation) {
        return;
      }
      unknownOutcome(statusEl, submitBtn, retryEl);
    });
  }

  function submitRequest(form, statusEl, retryEl, resultEl, submitBtn) {
    // Single-flight guard: no concurrent submits and no overlapping polls.
    if (state.busy) {
      return;
    }
    state.busy = true;
    stopPolling();
    var token = state.generation;
    if (submitBtn) {
      submitBtn.disabled = true;
    }
    statusEl.setAttribute("aria-busy", "true");
    showRetry(retryEl, false);
    setText(statusEl, message(statusEl, "sending",
      "Kérés küldése…", "Sending request…"));
    // The hidden field keeps the server-issued key; the player only reads it
    // and never writes it, so every retry reuses the SAME key.
    var keyField = form.querySelector('input[name="idempotency_key"]');
    if (!keyField || !keyField.value) {
      unknownOutcome(statusEl, submitBtn, retryEl);
      return;
    }
    fetch(form.getAttribute("action") || window.location.pathname, {
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-CSRFToken": readCookie("csrftoken") },
    }).then(asPayload, function () {
      return null;
    }).then(function (boxed) {
      if (token !== state.generation || !statusEl.isConnected) {
        return;
      }
      if (!boxed) {
        unknownOutcome(statusEl, submitBtn, retryEl);
        return;
      }
      var response = boxed.response;
      var payload = boxed.payload;
      if (!payload) {
        if (!wantsJson(response)) {
          // HTML answer means an auth redirect (login page): sign in again.
          setIdle(statusEl, submitBtn);
          setText(statusEl, message(statusEl, "session",
            "A munkamenet lejárt, kérjük, jelentkezzen be újra.",
            "Your session has expired, please log in again."));
          return;
        }
        unknownOutcome(statusEl, submitBtn, retryEl);
        return;
      }
      if (!response.ok) {
        setIdle(statusEl, submitBtn);
        var serverText = typeof payload.error === "string" && payload.error ? payload.error : "";
        setText(statusEl, serverText || message(statusEl, "failed",
          "A tanácskérés sikertelen.", "The advice request failed."));
        return;
      }
      if (payload.status === "pending" && payload.status_url) {
        state.statusUrl = payload.status_url;
        setText(statusEl, message(statusEl, "generating",
          "A tanács generálása folyamatban…", "Generating advice…"));
        startPoll(statusEl, retryEl, resultEl, payload.status_url, token, POLL_FIRST_MS, submitBtn);
        return;
      }
      if ((payload.status === "success" || payload.status === "failed") && payload.html) {
        finishTerminal(statusEl, submitBtn, retryEl, resultEl, payload,
          payload.status === "success" ? "done" : "failed");
        return;
      }
      if ((payload.status === "success" || payload.status === "failed") && payload.status_url) {
        state.statusUrl = payload.status_url;
        startPoll(statusEl, retryEl, resultEl, payload.status_url, token, POLL_FIRST_MS, submitBtn);
        return;
      }
      unknownOutcome(statusEl, submitBtn, retryEl);
    }, function () {
      if (token !== state.generation) {
        return;
      }
      unknownOutcome(statusEl, submitBtn, retryEl);
    });
  }

  function init() {
    var form = document.querySelector("[data-advice-form]");
    var pollBox = document.querySelector("[data-advice-poll]");
    var statusEl = document.querySelector("[data-advice-status]");
    var resultEl = document.querySelector("[data-advice-result]");
    var retryEl = document.querySelector("[data-advice-retry]");
    if (retryEl) {
      retryEl.addEventListener("click", function () {
        if (state.busy) {
          return;
        }
        retryEl.hidden = true;
        if (state.statusUrl && statusEl && resultEl) {
          // Explicit resume: poll the known run again, still no new POST.
          state.busy = true;
          var resumeBtn = form ? form.querySelector('button[type="submit"]') : null;
          if (resumeBtn) {
            resumeBtn.disabled = true;
          }
          state.generation += 1;
          var token = state.generation;
          statusEl.setAttribute("aria-busy", "true");
          setText(statusEl, message(statusEl, "waiting",
            "Várakozás az eredményre…", "Waiting for the result…"));
          startPoll(statusEl, retryEl, resultEl, state.statusUrl, token, POLL_FIRST_MS, resumeBtn);
        } else if (form && statusEl && resultEl) {
          // Explicit retry: one POST reusing the SAME hidden key.
          submitRequest(form, statusEl, retryEl, resultEl,
            form.querySelector('button[type="submit"]'));
        }
      });
    }
    if (form && statusEl && resultEl) {
      var submitBtn = form.querySelector('button[type="submit"]');
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        submitRequest(form, statusEl, retryEl, resultEl, submitBtn);
      });
    }
    if (pollBox && statusEl && resultEl) {
      // Reload recovery (detail page and request page alike): resume
      // polling the server-provided status_url of the existing pending
      // run rendered into this page. No client-persisted state, no synthesized
      // state: only server-returned status/phase is ever rendered.
      var url = pollBox.getAttribute("data-status-url") || "";
      if (url) {
        state.statusUrl = url;
        state.generation += 1;
        var resumeBtn = form ? form.querySelector('button[type="submit"]') : null;
        statusEl.setAttribute("aria-busy", "true");
        startPoll(statusEl, retryEl, resultEl, url, state.generation, POLL_FIRST_MS, resumeBtn);
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
