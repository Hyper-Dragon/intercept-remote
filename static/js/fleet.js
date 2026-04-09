/**
 * Fleet Capability Manager
 *
 * Fetches the aggregated capability model from /fleet/capabilities and
 * updates the navigation to reflect the live fleet state.
 *
 * In "remote" deployment mode:
 *   - Modes with no supporting agent are hidden entirely.
 *   - Modes where all agents are offline are shown as disabled.
 *   - Modes where agents are busy are shown with a busy badge.
 *   - Available modes are shown normally.
 *
 * In "local" deployment mode (legacy) the nav is left unchanged so the
 * experience is identical to a standalone SDR workstation.
 */
(function () {
  'use strict';

  /* ---- Configuration ---- */
  var POLL_INTERVAL_MS = 30000;   // 30 seconds
  var _pollTimer = null;
  var _lastCapabilities = null;

  /* ---- State classes applied to mode buttons ---- */
  var CLS_UNAVAILABLE = 'fleet-unavailable';
  var CLS_BUSY        = 'fleet-busy';
  var CLS_OFFLINE     = 'fleet-offline';
  var CLS_HIDDEN      = 'fleet-hidden';

  /* ---- Public API ---- */
  window.Fleet = {
    init: init,
    refresh: fetchCapabilities,
    getCapabilities: function () { return _lastCapabilities; },
    stop: stopPolling,
  };

  /**
   * Initialise the fleet manager.  Called once from the main app after
   * the DOM is ready.
   */
  function init() {
    fetchCapabilities();
    _pollTimer = setInterval(fetchCapabilities, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (_pollTimer) {
      clearInterval(_pollTimer);
      _pollTimer = null;
    }
  }

  /* ---- Core ---- */

  function fetchCapabilities() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/fleet/capabilities', true);
    xhr.timeout = 10000;
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var data = JSON.parse(xhr.responseText);
          _lastCapabilities = data;
          applyCapabilities(data);
          updateFleetBadge(data);
        } catch (e) {
          console.warn('[Fleet] Failed to parse capabilities:', e);
        }
      }
    };
    xhr.onerror = function () {
      console.warn('[Fleet] Capabilities request failed');
    };
    xhr.send();
  }

  /**
   * Walk the DOM nav and update each mode button based on fleet state.
   */
  function applyCapabilities(data) {
    // Build a lookup: mode -> state
    var modeStates = {};
    var groups = data.capabilities || [];
    for (var g = 0; g < groups.length; g++) {
      var caps = groups[g].capabilities || [];
      for (var c = 0; c < caps.length; c++) {
        modeStates[caps[c].mode] = caps[c].state;
      }
    }

    // Find all mode buttons (desktop + mobile)
    var buttons = document.querySelectorAll('[data-mode]');
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      var mode = btn.getAttribute('data-mode');
      var state = modeStates[mode];

      // Remove all fleet-state classes first
      btn.classList.remove(CLS_UNAVAILABLE, CLS_BUSY, CLS_OFFLINE, CLS_HIDDEN);

      if (!state || state === 'unsupported') {
        // No agent supports this mode – hide it
        btn.classList.add(CLS_HIDDEN);
        btn.style.display = 'none';
      } else if (state === 'offline') {
        // Agents support it but are all offline – show disabled
        btn.classList.add(CLS_OFFLINE);
        btn.style.display = '';
        btn.setAttribute('title', (btn.getAttribute('data-mode-label') || mode) + ' (agents offline)');
      } else if (state === 'busy') {
        // Agents support it but all busy
        btn.classList.add(CLS_BUSY);
        btn.style.display = '';
        btn.setAttribute('title', (btn.getAttribute('data-mode-label') || mode) + ' (agents busy)');
      } else {
        // Available
        btn.style.display = '';
        btn.setAttribute('title', btn.getAttribute('data-mode-label') || mode);
      }
    }

    // Hide empty dropdown groups
    var dropdowns = document.querySelectorAll('.mode-nav-dropdown');
    for (var d = 0; d < dropdowns.length; d++) {
      var menu = dropdowns[d].querySelector('.mode-nav-dropdown-menu');
      if (!menu) continue;
      var visibleItems = menu.querySelectorAll('[data-mode]:not([style*="display: none"])');
      if (visibleItems.length === 0) {
        dropdowns[d].style.display = 'none';
      } else {
        dropdowns[d].style.display = '';
      }
    }
  }

  /**
   * Update the fleet status badge in the nav bar.
   */
  function updateFleetBadge(data) {
    var badge = document.getElementById('fleetStatusBadge');
    var dot = document.getElementById('fleetStatusDot');
    var countEl = document.getElementById('fleetAgentCount');
    if (!badge) return;

    var online = data.online_count || 0;
    var total = data.agent_count || 0;

    if (countEl) countEl.textContent = online + '/' + total;

    if (dot) {
      dot.classList.remove('fleet-dot--online', 'fleet-dot--partial', 'fleet-dot--offline');
      if (total === 0) {
        dot.classList.add('fleet-dot--offline');
        badge.title = 'Fleet: no agents registered';
      } else if (online === 0) {
        dot.classList.add('fleet-dot--offline');
        badge.title = 'Fleet: all agents offline (' + total + ' registered)';
      } else if (online < total) {
        dot.classList.add('fleet-dot--partial');
        badge.title = 'Fleet: ' + online + ' of ' + total + ' agents online';
      } else {
        dot.classList.add('fleet-dot--online');
        badge.title = 'Fleet: all ' + total + ' agents online';
      }
    }
  }
})();
