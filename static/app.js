/**
 * Multi-Stream Listener & Transcriber — Dashboard Client
 *
 * Vanilla JavaScript driving the single-page dashboard.
 * Communicates with the FastAPI backend via REST API calls
 * and polls for status updates every 2 seconds.
 *
 * Requirements: 1.3–1.6, 2.2–2.4, 3.1, 3.3, 4.1, 4.2, 5.2, 5.3,
 *               6.3, 6.4, 7.1, 7.2, 12.1–12.5, 13.5, 15.1, 15.2
 */
(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // DOM references
  // -------------------------------------------------------------------------

  var btnStartAll = document.getElementById("btn-start-all");
  var btnPauseAll = document.getElementById("btn-pause-all");
  var concurrencyWarning = document.getElementById("concurrency-warning");
  var addStreamForm = document.getElementById("add-stream-form");
  var addBrandInput = document.getElementById("add-brand");
  var addUrlInput = document.getElementById("add-url");
  var addBrandError = document.getElementById("add-brand-error");
  var addUrlError = document.getElementById("add-url-error");
  var streamCardsContainer = document.getElementById("stream-cards");
  var noStreamsMessage = document.getElementById("no-streams-message");
  var streamCardTemplate = document.getElementById("stream-card-template");

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  /** @type {Array<Object>} Current list of streams from last poll */
  var streams = [];

  /** @type {number} Max concurrent streams from server config */
  var maxConcurrent = 5;

  /** @type {number|null} Polling interval ID */
  var pollIntervalId = null;

  /** Polling interval in milliseconds (Req 7.2) */
  var POLL_INTERVAL_MS = 2000;

  // -------------------------------------------------------------------------
  // Status badge configuration (Req 7.1)
  // -------------------------------------------------------------------------

  var STATUS_CONFIG = {
    idle: { label: "Idle", indicator: "\u25CB", badgeClass: "status-badge--idle" },
    listening: { label: "Listening", indicator: "\u25CF", badgeClass: "status-badge--listening" },
    reconnecting: { label: "Reconnecting", indicator: "\u25C9", badgeClass: "status-badge--reconnecting" },
    error: { label: "Error", indicator: "\u2715", badgeClass: "status-badge--error" },
  };

  var ALL_BADGE_CLASSES = Object.keys(STATUS_CONFIG).map(function (k) {
    return STATUS_CONFIG[k].badgeClass;
  });

  // -------------------------------------------------------------------------
  // API helpers
  // -------------------------------------------------------------------------

  /**
   * Generic fetch wrapper with JSON parsing and error handling.
   * @param {string} url
   * @param {RequestInit} [options]
   * @returns {Promise<{ok: boolean, data: any, error: string, status: number}>}
   */
  async function apiCall(url, options) {
    var opts = options || {};
    try {
      // Only set Content-Type for requests that carry a body
      if (opts.body) {
        opts.headers = Object.assign(
          { "Content-Type": "application/json" },
          opts.headers || {}
        );
      }
      var resp = await fetch(url, opts);
      var data = await resp.json().catch(function () {
        return null;
      });

      if (!resp.ok) {
        var message = (data && data.detail) || "Request failed (" + resp.status + ")";
        return { ok: false, error: message, status: resp.status };
      }
      return { ok: true, data: data, status: resp.status };
    } catch (err) {
      return { ok: false, error: err.message || "Network error", status: 0 };
    }
  }

  // -------------------------------------------------------------------------
  // Polling — GET /api/streams (Req 7.2: update within 2 seconds)
  // -------------------------------------------------------------------------

  async function fetchStatuses() {
    var result = await apiCall("/api/streams");
    if (!result.ok) return;

    streams = result.data.streams || [];
    maxConcurrent = result.data.max_concurrent_streams != null
      ? result.data.max_concurrent_streams
      : 5;
    renderAll();
  }

  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------

  function renderAll() {
    renderStreamCards();
    updateGlobalControls();
    updateConcurrencyWarning();
  }

  /**
   * Reconcile the DOM stream cards with the current `streams` array.
   * Creates new cards for new streams, updates existing ones, removes stale ones.
   */
  function renderStreamCards() {
    // Build a map of existing card elements keyed by stream ID
    var existingCards = {};
    streamCardsContainer.querySelectorAll(".stream-card").forEach(function (card) {
      existingCards[card.dataset.streamId] = card;
    });

    // Set of IDs present in the current data
    var currentIds = {};
    streams.forEach(function (s) {
      currentIds[s.id] = true;
    });

    // Remove cards that are no longer in the data
    Object.keys(existingCards).forEach(function (id) {
      if (!currentIds[id]) {
        existingCards[id].remove();
      }
    });

    // Create or update cards
    streams.forEach(function (stream) {
      var card = existingCards[stream.id];
      if (!card) {
        card = createStreamCard(stream);
        streamCardsContainer.appendChild(card);
      }
      updateStreamCard(card, stream);
    });

    // Show/hide the empty-state message
    noStreamsMessage.hidden = streams.length > 0;
  }

  /**
   * Clone the <template> and wire up event listeners for a new stream card.
   * @param {Object} stream - The stream data object
   * @returns {HTMLElement} The card element
   */
  function createStreamCard(stream) {
    var clone = streamCardTemplate.content.cloneNode(true);
    var card = clone.querySelector(".stream-card");
    var id = stream.id;
    card.dataset.streamId = id;
    card.setAttribute("aria-label", "Stream card for " + stream.brand);

    // Action buttons — look up current stream data from array at click time
    card.querySelector(".btn-play").addEventListener("click", function () {
      playStream(id);
    });
    card.querySelector(".btn-pause").addEventListener("click", function () {
      pauseStream(id);
    });
    card.querySelector(".btn-edit").addEventListener("click", function () {
      var current = findStream(id);
      if (current) toggleEditForm(card, current);
    });
    card.querySelector(".btn-delete").addEventListener("click", function () {
      var current = findStream(id);
      if (current) deleteStream(id, current.brand);
    });
    card.querySelector(".btn-download").addEventListener("click", function () {
      downloadTranscript(id);
    });

    // Edit form submission and cancel
    var editForm = card.querySelector(".edit-form");
    editForm.addEventListener("submit", function (e) {
      e.preventDefault();
      submitEdit(card, id);
    });
    card.querySelector(".btn-cancel-edit").addEventListener("click", function () {
      cancelEdit(card);
    });

    return card;
  }

  /**
   * Update an existing card's display to reflect current stream data.
   * Does not recreate or touch the edit form state, preserving user input.
   */
  function updateStreamCard(card, stream) {
    var state = stream.state || "idle";
    var isListening = state === "listening";
    var isReconnecting = state === "reconnecting";
    var isActive = isListening || isReconnecting;
    var atLimit = countListening() >= maxConcurrent;

    // -- Brand name and URL --
    card.querySelector('[data-field="brand"]').textContent = stream.brand;
    card.querySelector('[data-field="url"]').textContent = stream.url;

    // -- Status badge (Req 7.1) --
    var badge = card.querySelector('[data-field="status"]');
    var cfg = STATUS_CONFIG[state] || STATUS_CONFIG.idle;

    // Swap badge class
    ALL_BADGE_CLASSES.forEach(function (cls) {
      badge.classList.remove(cls);
    });
    badge.classList.add(cfg.badgeClass);

    // Badge text — include reconnect attempt count (Req 13.5)
    var badgeText = cfg.label;
    if (state === "reconnecting" && stream.reconnect_attempt != null) {
      badgeText = "Reconnecting " + stream.reconnect_attempt + "/5";
    }

    // Show extractor type for browser-based streams (Req 8.1)
    if (state === "listening" && stream.extractor_type === "browser") {
      badgeText = "Listening (browser)";
    }

    badge.innerHTML =
      '<span class="status-badge__indicator" aria-hidden="true">' +
      cfg.indicator +
      "</span> " +
      escapeHtml(badgeText);
    badge.setAttribute("aria-label", "Status: " + badgeText);

    // -- Error message (display API errors on card) --
    var errorEl = card.querySelector('[data-field="error"]');
    if (stream.error_message) {
      errorEl.textContent = stream.error_message;
      errorEl.hidden = false;
    } else {
      errorEl.textContent = "";
      errorEl.hidden = true;
    }

    // -- File path display (show when paused and has transcript) --
    var filePathEl = card.querySelector('.file-path-display');
    if (!filePathEl) {
      filePathEl = document.createElement('a');
      filePathEl.className = 'file-path-display';
      filePathEl.href = '#';
      filePathEl.style.cssText = 'display:block;font-size:0.8rem;color:#6366f1;margin:0.5rem 0 0;word-break:break-all;cursor:pointer;text-decoration:underline;';
      filePathEl.title = 'Click to open folder in file explorer';
      filePathEl.addEventListener('click', function(e) {
        e.preventDefault();
        var p = this.dataset.filePath;
        if (p) {
          fetch('/api/open-folder', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path: p})
          }).catch(function(){});
        }
      });
      errorEl.parentNode.insertBefore(filePathEl, errorEl.nextSibling);
    }
    if (!isActive && stream.daily_file_path) {
      filePathEl.dataset.filePath = stream.daily_file_path;
      filePathEl.innerHTML = '\uD83D\uDCC1 ' + escapeHtml(stream.daily_file_path);
      filePathEl.hidden = false;
    } else {
      filePathEl.hidden = true;
    }

    // -- Button visibility and enabled state --
    var btnPlay = card.querySelector(".btn-play");
    var btnPause = card.querySelector(".btn-pause");
    var btnEdit = card.querySelector(".btn-edit");
    var btnDelete = card.querySelector(".btn-delete");
    var btnDownload = card.querySelector(".btn-download");

    // Play/Pause toggle
    btnPlay.hidden = isActive;
    btnPause.hidden = !isActive;

    // Play disabled at concurrency limit (Req 5.2)
    if (!isActive && atLimit) {
      btnPlay.disabled = true;
      btnPlay.setAttribute("aria-disabled", "true");
    } else {
      btnPlay.disabled = false;
      btnPlay.removeAttribute("aria-disabled");
    }

    // Edit/Delete disabled while listening or reconnecting (Req 2.3, 3.3)
    btnEdit.disabled = isActive;
    btnDelete.disabled = isActive;
    if (isActive) {
      btnEdit.setAttribute("aria-disabled", "true");
      btnEdit.title = "Pause stream before editing";
      btnDelete.setAttribute("aria-disabled", "true");
      btnDelete.title = "Pause stream before removing";
    } else {
      btnEdit.removeAttribute("aria-disabled");
      btnEdit.title = "";
      btnDelete.removeAttribute("aria-disabled");
      btnDelete.title = "";
    }

    // Download button — disabled if no transcript exists (Req 12.4)
    if (!stream.has_transcript) {
      btnDownload.disabled = true;
      btnDownload.setAttribute("aria-disabled", "true");
      btnDownload.title = "No transcript available";
    } else {
      btnDownload.disabled = false;
      btnDownload.removeAttribute("aria-disabled");
      btnDownload.title = "";
    }
  }

  // -------------------------------------------------------------------------
  // Global controls (Req 6.3, 6.4)
  // -------------------------------------------------------------------------

  /** Count streams in an active state (listening or reconnecting). */
  function countListening() {
    return streams.filter(function (s) {
      return s.state === "listening" || s.state === "reconnecting";
    }).length;
  }

  /** Enable/disable Start All and Pause All based on current stream states. */
  function updateGlobalControls() {
    var listeningCount = countListening();
    var atLimit = listeningCount >= maxConcurrent;
    var hasIdleStreams = streams.some(function (s) {
      return s.state === "idle" || s.state === "error";
    });

    // Start All: disabled if at limit or no startable streams (Req 6.3)
    btnStartAll.disabled = !hasIdleStreams || atLimit;

    // Pause All: disabled if nothing is listening (Req 6.4)
    btnPauseAll.disabled = listeningCount === 0;
  }

  /** Show or hide the concurrency limit warning banner (Req 5.2). */
  function updateConcurrencyWarning() {
    var atLimit = countListening() >= maxConcurrent && streams.length > 0;

    if (atLimit) {
      concurrencyWarning.hidden = false;
      concurrencyWarning.classList.add("warning-banner--visible");
    } else {
      concurrencyWarning.hidden = true;
      concurrencyWarning.classList.remove("warning-banner--visible");
    }
  }

  // -------------------------------------------------------------------------
  // Validation helpers
  // -------------------------------------------------------------------------

  /**
   * Validate a brand name. Returns an error message or empty string.
   * @param {string} brand
   * @param {number} maxLen - Maximum allowed length
   */
  function validateBrand(brand, maxLen) {
    var trimmed = (brand || "").trim();
    if (!trimmed) return "Brand name is required";
    if (trimmed.length > maxLen) return "Brand name must be at most " + maxLen + " characters";
    return "";
  }

  /**
   * Validate a URL using the URL constructor (Req 1.5).
   * Returns an error message or empty string.
   */
  function validateUrl(url) {
    var trimmed = (url || "").trim();
    if (!trimmed) return "Stream URL is required";
    try {
      new URL(trimmed);
    } catch (e) {
      return "Invalid URL format";
    }
    return "";
  }

  /**
   * Check if a URL is already configured on another stream (Req 1.6).
   * @param {string} url
   * @param {string|null} excludeId - Stream ID to exclude (for edit)
   */
  function checkDuplicateUrl(url, excludeId) {
    var trimmed = (url || "").trim();
    var dup = streams.find(function (s) {
      return s.url === trimmed && s.id !== excludeId;
    });
    return dup ? "This URL is already configured" : "";
  }

  /**
   * Check if a brand name is already used by another stream (Req 2.4).
   * @param {string} brand
   * @param {string|null} excludeId - Stream ID to exclude (for edit)
   */
  function checkDuplicateBrand(brand, excludeId) {
    var trimmed = (brand || "").trim();
    var dup = streams.find(function (s) {
      return s.brand === trimmed && s.id !== excludeId;
    });
    return dup ? "This brand name is already in use" : "";
  }

  /**
   * Display or clear a validation error on a form field.
   * @param {HTMLElement} errorEl - The error message span
   * @param {HTMLElement} inputEl - The associated input element
   * @param {string} message - Error message or empty string to clear
   */
  function setFieldError(errorEl, inputEl, message) {
    errorEl.textContent = message;
    if (message) {
      inputEl.classList.add("form-input--error");
    } else {
      inputEl.classList.remove("form-input--error");
    }
  }

  // -------------------------------------------------------------------------
  // Utility helpers
  // -------------------------------------------------------------------------

  /** Find a stream object by ID in the current streams array. */
  function findStream(id) {
    return streams.find(function (s) {
      return s.id === id;
    }) || null;
  }

  /** Minimal HTML escaping for safe text insertion. */
  function escapeHtml(text) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(text));
    return div.innerHTML;
  }

  // -------------------------------------------------------------------------
  // Add stream (Req 1.3–1.6)
  // -------------------------------------------------------------------------

  addStreamForm.addEventListener("submit", async function (e) {
    e.preventDefault();

    var brand = addBrandInput.value;
    var url = addUrlInput.value;

    // Client-side validation
    var brandErr = validateBrand(brand, 100);
    var urlErr = validateUrl(url) || checkDuplicateUrl(url, null);

    setFieldError(addBrandError, addBrandInput, brandErr);
    setFieldError(addUrlError, addUrlInput, urlErr);

    if (brandErr || urlErr) return;

    var result = await apiCall("/api/streams", {
      method: "POST",
      body: JSON.stringify({ brand: brand.trim(), url: url.trim() }),
    });

    if (!result.ok) {
      // Display server-side error on the form
      setFieldError(addUrlError, addUrlInput, result.error);
      return;
    }

    // Clear form on success (Req 1.3)
    addStreamForm.reset();
    setFieldError(addBrandError, addBrandInput, "");
    setFieldError(addUrlError, addUrlInput, "");

    // Refresh immediately so the new card appears without waiting for poll
    await fetchStatuses();
  });

  // -------------------------------------------------------------------------
  // Edit stream (Req 2.2–2.4)
  // -------------------------------------------------------------------------

  /**
   * Toggle the inline edit form on a stream card.
   * Populates inputs with current values when opening.
   */
  function toggleEditForm(card, stream) {
    var editForm = card.querySelector(".edit-form");
    var isHidden = editForm.hidden;

    if (isHidden) {
      // Populate with current values
      card.querySelector(".edit-brand").value = stream.brand;
      card.querySelector(".edit-url").value = stream.url;

      // Clear previous errors
      var brandErrEl = card.querySelector(".edit-brand-error");
      var urlErrEl = card.querySelector(".edit-url-error");
      brandErrEl.textContent = "";
      urlErrEl.textContent = "";
      card.querySelector(".edit-brand").classList.remove("form-input--error");
      card.querySelector(".edit-url").classList.remove("form-input--error");

      editForm.hidden = false;
    } else {
      editForm.hidden = true;
    }
  }

  /** Close the edit form without saving. */
  function cancelEdit(card) {
    card.querySelector(".edit-form").hidden = true;
  }

  /** Validate and submit the edit form for a stream. */
  async function submitEdit(card, streamId) {
    var brandInput = card.querySelector(".edit-brand");
    var urlInput = card.querySelector(".edit-url");
    var brandErrEl = card.querySelector(".edit-brand-error");
    var urlErrEl = card.querySelector(".edit-url-error");

    var brand = brandInput.value;
    var url = urlInput.value;

    // Client-side validation (Req 2.4)
    var brandErr = validateBrand(brand, 50) || checkDuplicateBrand(brand, streamId);
    var urlErr = validateUrl(url) || checkDuplicateUrl(url, streamId);

    setFieldError(brandErrEl, brandInput, brandErr);
    setFieldError(urlErrEl, urlInput, urlErr);

    if (brandErr || urlErr) return;

    var result = await apiCall("/api/streams/" + streamId, {
      method: "PUT",
      body: JSON.stringify({ brand: brand.trim(), url: url.trim() }),
    });

    if (!result.ok) {
      // Show server error on the edit form
      setFieldError(urlErrEl, urlInput, result.error);
      return;
    }

    // Close form and refresh (Req 2.2)
    card.querySelector(".edit-form").hidden = true;
    await fetchStatuses();
  }

  // -------------------------------------------------------------------------
  // Delete stream (Req 3.1, 3.3)
  // -------------------------------------------------------------------------

  async function deleteStream(streamId, brandName) {
    // Browser confirmation prompt identifying the stream by name (Req 3.1)
    if (!confirm('Remove stream "' + brandName + '"?')) return;

    var result = await apiCall("/api/streams/" + streamId, {
      method: "DELETE",
    });

    if (!result.ok) {
      showCardError(streamId, result.error);
      return;
    }

    await fetchStatuses();
  }

  // -------------------------------------------------------------------------
  // Play / Pause individual stream (Req 4.1, 4.2)
  // -------------------------------------------------------------------------

  async function playStream(streamId) {
    var result = await apiCall("/api/streams/" + streamId + "/start", {
      method: "POST",
    });

    if (!result.ok) {
      showCardError(streamId, result.error);
    }

    await fetchStatuses();
  }

  async function pauseStream(streamId) {
    var result = await apiCall("/api/streams/" + streamId + "/stop", {
      method: "POST",
    });

    if (!result.ok) {
      showCardError(streamId, result.error);
    }

    await fetchStatuses();
  }

  // -------------------------------------------------------------------------
  // Start All / Pause All (Req 6.1–6.4)
  // -------------------------------------------------------------------------

  btnStartAll.addEventListener("click", async function () {
    btnStartAll.disabled = true;

    var result = await apiCall("/api/streams/start-all", { method: "POST" });

    if (!result.ok) {
      await fetchStatuses();
      return;
    }

    // Show errors for streams that failed to start (Req 6.5)
    if (result.data && result.data.failed && result.data.failed.length) {
      result.data.failed.forEach(function (fail) {
        showCardError(fail.id, fail.error || "Failed to start");
      });
    }

    await fetchStatuses();
  });

  btnPauseAll.addEventListener("click", async function () {
    btnPauseAll.disabled = true;
    await apiCall("/api/streams/stop-all", { method: "POST" });
    await fetchStatuses();
  });

  // -------------------------------------------------------------------------
  // Download transcript (Req 12.1–12.5)
  // -------------------------------------------------------------------------

  /**
   * Download the transcript file for a stream by creating a temporary
   * anchor element with a download attribute (Req 12.2, 12.3).
   */
  async function downloadTranscript(streamId) {
    try {
      var resp = await fetch("/api/streams/" + streamId + "/download");

      if (!resp.ok) {
        var data = await resp.json().catch(function () {
          return null;
        });
        var msg = (data && data.detail) || "Download failed";
        showCardError(streamId, msg);
        return;
      }

      // Extract filename from Content-Disposition header if present
      var disposition = resp.headers.get("content-disposition") || "";
      var filenameMatch = disposition.match(/filename="?([^"]+)"?/);
      var filename = filenameMatch
        ? filenameMatch[1]
        : "transcript_" + streamId + ".txt";

      // Create a temporary anchor to trigger the browser's file download
      var blob = await resp.blob();
      var objectUrl = URL.createObjectURL(blob);
      var anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(objectUrl);
    } catch (err) {
      showCardError(streamId, "Download failed: " + err.message);
    }
  }

  // -------------------------------------------------------------------------
  // Card error display helper
  // -------------------------------------------------------------------------

  /** Show an error message on a specific stream card. */
  function showCardError(streamId, message) {
    var card = streamCardsContainer.querySelector(
      '.stream-card[data-stream-id="' + streamId + '"]'
    );
    if (!card) return;
    var errorEl = card.querySelector('[data-field="error"]');
    errorEl.textContent = message;
    errorEl.hidden = false;
  }

  // -------------------------------------------------------------------------
  // Initialisation
  // -------------------------------------------------------------------------

  async function init() {
    // Perform initial fetch to populate the dashboard
    await fetchStatuses();

    // Start polling for status updates (Req 7.2)
    pollIntervalId = setInterval(fetchStatuses, POLL_INTERVAL_MS);
  }

  init();
})();
