/* The settings page.
 *
 * Two ideas run through this file.
 *
 * The first is that the page should ask for the little that a person
 * actually has - a Hive login, an Octopus API key, a postcode - and let
 * Ember work out everything else by talking to the services. Nothing
 * here asks for a meter point or a tariff code.
 *
 * The second is that a page like this gets opened, typed into and then
 * closed. So the form is remembered in this browser's local storage, and
 * the server keeps its own copy of the settings that are actually in
 * use. The server's copy always wins on load: what you see is what
 * Ember is really using.
 */

(function () {
  "use strict";

  var STORE = "ember.settings.v1";

  var TEXT = ["hive_email", "octopus_account", "postcode", "panel_name"];
  var SECRETS = ["hive_password", "octopus_api_key"];
  var NUMBERS = ["monthly_budget", "base_temperature", "calorific_value",
                 "poll_seconds", "heating_minutes_threshold", "rotation_seconds"];
  var CHOICES = ["gas_unit", "clock_24h"];

  // -----------------------------------------------------------------------
  // small helpers
  // -----------------------------------------------------------------------

  function el(id) { return document.getElementById(id); }

  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function readStore() {
    try {
      var raw = window.localStorage.getItem(STORE);
      return raw ? JSON.parse(raw) : {};
    } catch (err) { return {}; }
  }

  function writeStore(values) {
    try {
      window.localStorage.setItem(STORE, JSON.stringify(values));
    } catch (err) { /* private browsing, or the disk is full. Not fatal. */ }
  }

  function forgetStore() {
    try { window.localStorage.removeItem(STORE); } catch (err) { /* ignore */ }
  }

  function api(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().catch(function () {
        return { ok: false, error: "the server sent something unreadable" };
      }).then(function (data) {
        if (!response.ok && !data.error) {
          data = { ok: false, error: "the server said " + response.status };
        }
        return data;
      });
    });
  }

  function result(id, html, kind) {
    var box = el(id);
    if (!box) { return; }
    box.innerHTML = html;
    box.className = "result " + (kind || "");
  }

  function busy(button, on) {
    if (!button) { return; }
    button.disabled = !!on;
    button.classList.toggle("busy", !!on);
  }

  // -----------------------------------------------------------------------
  // the form
  // -----------------------------------------------------------------------

  var state = { settings: {}, stored: readStore() };

  function fieldValue(name) {
    var input = el(name);
    if (!input) { return ""; }
    if (input.type === "checkbox") { return input.checked; }
    return input.value;
  }

  function setField(name, value) {
    var input = el(name);
    if (!input || value === undefined || value === null) { return; }
    if (input.type === "checkbox") { input.checked = !!value; return; }
    input.value = value;
  }

  function remember() {
    var values = readStore();
    TEXT.concat(SECRETS).concat(NUMBERS).concat(CHOICES).forEach(function (name) {
      var input = el(name);
      if (input) { values[name] = fieldValue(name); }
    });
    var slides = [];
    document.querySelectorAll('input[name="slide"]').forEach(function (box) {
      if (box.checked) { slides.push(box.value); }
    });
    values.slides = slides;
    writeStore(values);
    state.stored = values;
  }

  function fill(settings) {
    // The server's copy is the truth: it is what Ember is actually
    // using. Local storage only fills the gaps the server has nothing
    // for, which is where something was typed but never saved.
    TEXT.concat(NUMBERS).concat(CHOICES).forEach(function (name) {
      var stored = state.stored[name];
      var fromServer = settings[name];
      if (fromServer !== undefined && fromServer !== null && fromServer !== "") {
        setField(name, fromServer);
      } else if (stored !== undefined && stored !== null && stored !== "") {
        setField(name, stored);
      }
    });
    SECRETS.forEach(function (name) {
      var stored = state.stored[name];
      if (stored) { setField(name, stored); }
    });
    if (settings.clock_24h !== undefined) {
      setField("clock_24h", settings.clock_24h ? "true" : "false");
    }
    var slides = settings.slides || state.stored.slides;
    if (slides && slides.length) {
      document.querySelectorAll('input[name="slide"]').forEach(function (box) {
        box.checked = slides.indexOf(box.value) >= 0;
      });
    }
  }

  // -----------------------------------------------------------------------
  // what is set up
  // -----------------------------------------------------------------------

  function applyStatus(data) {
    state.settings = data.settings || {};
    fill(state.settings);
    var missing = data.missing || [];
    var banner = el("banner");
    if (!missing.length) {
      banner.className = "banner good";
      banner.innerHTML = "<b>Everything is set up.</b> Ember is talking to " +
        "Hive and Octopus.";
    } else {
      banner.className = "banner warn";
      banner.innerHTML = "Still to do: <b>" + esc(missing.join(", ")) +
        "</b>. Fill in what is missing below and press the button next to it.";
    }
    /* "signed in" means Hive answered the last poll. Having the login
       saved, or a session file on disk, is not the same thing - which
       is why the badge says which one it is. */
    var hive = data.hive || {};
    var session = (data.files || []).filter(function (file) {
      return file.name === "Hive session";
    })[0];
    var signedIn = !!hive.signed_in;
    var badge = signedIn ? "signed in"
      : (hive.problem ? "not working"
        : (session && session.exists ? "session saved"
          : (state.settings.has_hive_password ? "credentials saved" : "not signed in")));
    el("hive-state").textContent = badge;
    el("hive-state").className = "state" +
      (signedIn || state.settings.has_hive_password ? " ok" : "");
    var hint = el("hive-hint");
    if (hint) {
      hint.textContent = hive.problem || "";
      hint.hidden = !hive.problem;
    }
    el("octopus-state").textContent = state.settings.gas_mprn
      ? ("MPRN " + state.settings.gas_mprn) : "not set up";
    el("octopus-state").className = "state" +
      (state.settings.gas_mprn ? " ok" : "");
    showFiles(data.files || []);
    showHistory(data.history || {});
  }

  /* The history store, so "is it going to fill the card?" has an answer
     with a number in it rather than a feeling. */
  function showHistory(history) {
    var box = el("history");
    var rows = history.rows || {};
    var days = history.history_days;
    var trvDays = history.trv_days;
    if (el("history-days")) { el("history-days").textContent = days || "?"; }
    if (el("trv-days")) { el("trv-days").textContent = trvDays || "?"; }
    if (!box) { return; }
    if (!rows || (!rows.polls && !rows.trv && !rows.gas)) {
      box.innerHTML = "";
      var badge = el("history-state");
      if (badge) { badge.textContent = "no history yet"; badge.className = "state"; }
      return;
    }
    var perDay = history.rows_per_day || {};
    box.innerHTML = [
      row("Gas readings", (rows.gas || 0).toLocaleString() + " rows"),
      row("Heating polls", (rows.polls || 0).toLocaleString() + " rows"),
      row("Room changes", (rows.trv || 0).toLocaleString() + " rows"),
      row("On disk", bytes(history.bytes || 0)),
      row("Growth", (perDay.polls || 0) + " polls and " +
        (perDay.gas || 0) + " gas rows a day, plus a few room changes")
    ].join("");
    var state = el("history-state");
    if (state) {
      state.textContent = bytes(history.bytes || 0);
      state.className = "state ok";
    }
  }

  function row(name, value) {
    return '<div><b>' + esc(name) + "</b> &nbsp;" + esc(value) + "</div>";
  }

  function bytes(count) {
    if (!count) { return "0 bytes"; }
    if (count < 1024) { return count + " bytes"; }
    if (count < 1024 * 1024) { return (count / 1024).toFixed(1) + " kB"; }
    return (count / 1024 / 1024).toFixed(1) + " MB";
  }

  function pruneHistory() {
    var button = el("history-prune");
    busy(button, true);
    api("/api/history/prune", {}).then(function (data) {
      busy(button, false);
      if (!data.ok) {
        result("history-result", '<span class="bad">' +
          esc(data.error || "that did not work") + "</span>", "bad");
        return;
      }
      result("history-result", '<span class="ok">Tidied up. ' +
        (data.removed || 0) + " old rows deleted.</span>", "ok");
      if (data.history) { showHistory(data.history); }
      refreshStatus();
    }).catch(function (err) {
      busy(button, false);
      result("history-result", '<span class="bad">' +
        esc(err.message || err) + "</span>", "bad");
    });
  }

  function load() {
    fetch("/api/discover/status").then(function (r) { return r.json(); })
      .then(applyStatus)
      .catch(function () {
        el("banner").className = "banner warn";
        el("banner").textContent = "Could not reach Ember. Is it still running?";
      });
  }

  /* Read the status again. The banner and the file list are a snapshot
     of the world at that moment, so they have to be re-read after
     something has changed the world. */
  function refreshStatus() {
    fetch("/api/discover/status").then(function (r) { return r.json(); })
      .then(applyStatus)
      .catch(function () { /* the banner will be wrong, but not fatal */ });
  }

  function showFiles(files) {
    var box = el("files");
    if (!files || !files.length) { box.innerHTML = ""; return; }
    box.innerHTML = files.map(function (file) {
      return '<div class="' + (file.exists ? "" : "missing") + '"><b>' +
        esc(file.name) + "</b> &nbsp;" + esc(file.path) +
        (file.exists ? "" : " &nbsp;(not there yet)") + "</div>";
    }).join("");
    /* The first Hive session path is the one Ember writes to. The rest
       are places it will also look, in case the session was copied in
       from somewhere else. */
  }

  // -----------------------------------------------------------------------
  // Hive
  // -----------------------------------------------------------------------

  function signIn() {
    var button = el("hive-signin");
    var email = el("hive_email").value.trim();
    var password = el("hive_password").value;
    /* The password is never sent to the browser, so the box is empty
       every time - and if one is already saved, an empty box means
       "use the one you have". Without this, signing in again after a
       session dies means retyping a password that is already on the
       server. */
    var savedPassword = !!state.settings.has_hive_password;
    if (!email && state.settings.hive_email) {
      email = state.settings.hive_email;
    }
    if (!email || (!password && !savedPassword)) {
      result("hive-result",
             "An email address is needed, and a password unless one is already saved.",
             "bad");
      return;
    }
    remember();
    busy(button, true);
    api("/api/discover/hive", { email: email, password: password })
      .then(function (data) {
        busy(button, false);
        if (!data.ok) {
          el("hive-state").textContent = "not signed in";
          el("hive-state").className = "state bad";
          result("hive-result", '<span class="bad">' + esc(data.error) + "</span>", "bad");
          return;
        }
        var outcome = data.result || {};
        if (outcome.status === "needs_code") {
          // The flow id is what ties the code back to this sign-in.
          state.flow = outcome.flow;
          el("hive-code").hidden = false;
          el("hive-code-input").focus();
          result("hive-result",
            '<span class="dim">' + esc(outcome.message || "Hive has sent a code.") +
            "</span>", "");
          return;
        }
        el("hive-code").hidden = true;
        forgetSecret("hive_password");
        el("hive-state").textContent = "signed in";
        el("hive-state").className = "state ok";
        var rooms = outcome.rooms || [];
        var list = rooms.length
          ? "<ul>" + rooms.map(function (room) {
              return "<li>" + esc(room) + "</li>";
            }).join("") + "</ul>"
          : "";
        result("hive-result",
          '<span class="ok">Signed in.</span> ' + rooms.length +
          " room" + (rooms.length === 1 ? "" : "s") + " found." + list +
          (outcome.device ? ' <span class="dim">' + esc(outcome.device) + "</span>" : "") +
          sessionNote(outcome),
          "ok");
        refreshStatus();
      })
      .catch(function (err) {
        busy(button, false);
        result("hive-result", '<span class="bad">' + esc(err.message || err) + "</span>", "bad");
      });
  }

  /* Where the session was saved. Without this the page says "signed
     in" and then the dashboard says "not signed in", and there is no
     way to tell that the two are talking about different files. */
  function sessionNote(outcome) {
    if (!outcome || !outcome.session_file) return "";
    return '<br><span class="dim">Session saved to <code>' +
      esc(outcome.session_file) + "</code></span>";
  }

  function sendCode() {
    var button = el("hive-code-send");
    var code = el("hive_code_input").value.trim();
    var flow = state.flow;
    if (!code) {
      result("hive-result", "The code was empty.", "bad");
      return;
    }
    busy(button, true);
    api("/api/discover/hive/code", { flow: flow, code: code })
      .then(function (data) {
        busy(button, false);
        if (!data.ok) {
          result("hive-result", '<span class="bad">' + esc(data.error) + "</span>", "bad");
          return;
        }
        var outcome = data.result || {};
        if (outcome.status !== "signed_in") {
          // Hive asked for something else. Say so rather than claiming
          // a sign-in that did not happen.
          el("hive-code").hidden = false;
          result("hive-result",
            '<span class="bad">' + esc(outcome.message ||
              "Hive is not finished with the sign-in.") + "</span>", "bad");
          return;
        }
        el("hive-code").hidden = true;
        el("hive_code_input").value = "";
        forgetSecret("hive_password");
        el("hive-state").textContent = "signed in";
        el("hive-state").className = "state ok";
        var rooms = outcome.rooms || [];
        result("hive-result",
          '<span class="ok">Signed in.</span> ' + rooms.length +
          " room" + (rooms.length === 1 ? "" : "s") + " found." +
          (outcome.device ? ' <span class="dim">' + esc(outcome.device) + "</span>" : "") +
          sessionNote(outcome),
          "ok");
        refreshStatus();
      })
      .catch(function (err) {
        busy(button, false);
        result("hive-result", '<span class="bad">' + esc(err.message || err) + "</span>", "bad");
      });
  }

  // -----------------------------------------------------------------------
  // Octopus
  // -----------------------------------------------------------------------

  function lookUpOctopus() {
    var button = el("octopus-look");
    var key = el("octopus_api_key").value.trim();
    if (!key) {
      result("octopus-result", "An Octopus API key is needed.", "bad");
      return;
    }
    remember();
    busy(button, true);
    api("/api/discover/octopus", {
      api_key: key,
      account: el("octopus_account").value.trim()
    })
      .then(function (data) {
        busy(button, false);
        if (!data.ok) {
          el("octopus-state").textContent = "not set up";
          el("octopus-state").className = "state bad";
          result("octopus-result", '<span class="bad">' + esc(data.error) + "</span>", "bad");
          return;
        }
        forgetSecret("octopus_api_key");
        var s = data.summary || {};
        el("octopus-state").textContent = "MPRN " + (s.mprn || "?");
        el("octopus-state").className = "state ok";
        fill(data.settings || {});
        var rows = [];
        rows.push("<li>Account <code>" + esc(s.account || "?") + "</code></li>");
        rows.push("<li>Meter point <code>" + esc(s.mprn || "?") + "</code></li>");
        (s.serials || []).forEach(function (serial) {
          rows.push("<li>Meter <code>" + esc(serial) + "</code>" +
            (s.meter_type ? " &middot; " + esc(s.meter_type) : "") + "</li>");
        });
        rows.push("<li>Tariff <code>" + esc(s.product_code || "?") + "</code> / <code>" +
          esc(s.tariff_code || "?") + "</code>" +
          (s.tariff_name ? " &middot; " + esc(s.tariff_name) : "") + "</li>");
        if (s.unit_rate_p !== undefined && s.unit_rate_p !== null) {
          rows.push("<li>Unit rate <code>" + esc(s.unit_rate_p) +
            "</code> p/kWh, standing charge <code>" + esc(s.standing_charge_p) +
            "</code> p/day</li>");
        }
        var unit = s.unit
          ? "The meter reads in " + esc(s.unit === "m3" ? "cubic metres" : "kWh") +
            " - " + esc(s.unit_reason) + "."
          : esc(s.unit_reason || "The meter does not say what it measures.");
        var notes = (s.notes || []).map(function (note) {
          return '<span class="bad">' + esc(note) + "</span>";
        }).join("<br>");
        var extra = s.unit_reset || "";
        if (extra) {
          notes = notes
            ? notes + "<br>" + '<span class="dim">' + esc(extra) + "</span>"
            : '<span class="dim">' + esc(extra) + "</span>";
        }
        result("octopus-result",
          '<span class="ok">Found it.</span> <ul>' + rows.join("") + "</ul>" +
          '<span class="dim">' + esc(unit) + "</span>" +
          (notes ? "<br>" + notes : ""), notes ? "bad" : "ok");
        refreshStatus();
      })
      .catch(function (err) {
        busy(button, false);
        result("octopus-result", '<span class="bad">' + esc(err.message || err) + "</span>", "bad");
      });
  }

  // -----------------------------------------------------------------------
  // Where the house is
  // -----------------------------------------------------------------------

  function lookUpPlace() {
    var button = el("place-look");
    var postcode = el("postcode").value.trim();
    if (!postcode) {
      result("place-result", "A postcode is needed.", "bad");
      return;
    }
    remember();
    busy(button, true);
    api("/api/discover/place", { postcode: postcode })
      .then(function (data) {
        busy(button, false);
        if (!data.ok) {
          result("place-result", '<span class="bad">' + esc(data.error) + "</span>", "bad");
          return;
        }
        var s = data.summary || {};
        fill(data.settings || {});
        result("place-result",
          '<span class="ok">Found it.</span> <span class="dim">' +
          esc(s.name || "") + (s.country ? ", " + esc(s.country) : "") +
          " &middot; " + esc(s.latitude) + ", " + esc(s.longitude) +
          "</span>", "ok");
        refreshStatus();
      })
      .catch(function (err) {
        busy(button, false);
        result("place-result", '<span class="bad">' + esc(err.message || err) + "</span>", "bad");
      });
  }

  // -----------------------------------------------------------------------
  // Saving plain settings
  // -----------------------------------------------------------------------

  function collectNumbers() {
    var values = {};
    NUMBERS.forEach(function (name) {
      var raw = el(name).value;
      if (raw === "" || raw === null) { return; }
      var number = Number(raw);
      if (!isNaN(number)) { values[name] = number; }
    });
    if (el("gas_unit").value) { values.gas_unit = el("gas_unit").value; }
    return values;
  }

  function saveNumbers() {
    var button = el("numbers-save");
    remember();
    busy(button, true);
    api("/api/settings", collectNumbers())
      .then(function (data) {
        busy(button, false);
        if (!data.ok) {
          result("numbers-result", '<span class="bad">' +
            esc(data.error || "could not save") + "</span>", "bad");
          return;
        }
        result("numbers-result", '<span class="ok">Saved.</span>', "ok");
      })
      .catch(function (err) {
        busy(button, false);
        result("numbers-result", '<span class="bad">' + esc(err.message || err) + "</span>", "bad");
      });
  }

  function collectDisplay() {
    var slides = [];
    document.querySelectorAll('input[name="slide"]').forEach(function (box) {
      if (box.checked) { slides.push(box.value); }
    });
    return {
      panel_name: el("panel_name").value,
      rotation_seconds: Number(el("rotation_seconds").value) || 0,
      clock_24h: el("clock_24h").value === "true",
      slides: slides
    };
  }

  function saveDisplay() {
    var button = el("display-save");
    remember();
    busy(button, true);
    api("/api/settings", collectDisplay())
      .then(function (data) {
        busy(button, false);
        if (!data.ok) {
          result("display-result", '<span class="bad">' +
            esc(data.error || "could not save") + "</span>", "bad");
          return;
        }
        result("display-result", '<span class="ok">Saved.</span>', "ok");
      })
      .catch(function (err) {
        busy(button, false);
        result("display-result", '<span class="bad">' + esc(err.message || err) + "</span>", "bad");
      });
  }

  // -----------------------------------------------------------------------
  // the machine
  // -----------------------------------------------------------------------

  function refreshNow() {
    var button = el("refresh-now");
    busy(button, true);
    api("/api/discover/refresh", {})
      .then(function (data) {
        busy(button, false);
        var errors = (data.state && data.state.errors) || [];
        if (!data.ok) {
          result("files-result", '<span class="bad">' +
            esc(data.error || "could not fetch") + "</span>", "bad");
          return;
        }
        if (errors.length) {
          result("files-result",
            '<span class="bad">Fetched, but ' + errors.length + " thing" +
            (errors.length === 1 ? "" : "s") + " did not come back:</span><ul>" +
            errors.map(function (item) {
              return "<li>" + esc(item.message || item) + "</li>";
            }).join("") + "</ul>", "bad");
          return;
        }
        result("files-result", '<span class="ok">Fetched everything.</span>', "ok");
      })
      .catch(function (err) {
        busy(button, false);
        result("files-result", '<span class="bad">' + esc(err.message || err) + "</span>", "bad");
      });
  }

  /* Once the server has it, the browser does not need to keep it.

     A password or an API key is worth remembering while it is being
     typed - that is what stops a reload throwing the work away. Once
     the sign-in or the lookup has worked, the secret lives in the
     settings file with the rest of it, and the browser's copy is a
     liability rather than a convenience.

     Three things have to be cleared, not one: the stored copy, the
     in-memory copy that `fill()` restores from, and the field itself.
     Clear only the stored copy and the next keystroke anywhere on the
     page puts it straight back. */
  function forgetSecret(name) {
    var values = readStore();
    delete values[name];
    writeStore(values);
    state.stored[name] = "";
    var input = el(name);
    if (input) {
      input.value = "";
      input.placeholder = "saved on the server";
    }
  }

  function forget() {
    forgetStore();
    var fields = TEXT.concat(SECRETS).concat(NUMBERS).concat(CHOICES);
    fields.forEach(function (name) { setField(name, ""); });
    document.querySelectorAll('input[name="slide"]').forEach(function (box) {
      box.checked = false;
    });
    result("files-result",
      '<span class="ok">This browser has forgotten what it was told.</span> ' +
      '<span class="dim">Ember\'s own settings are untouched.</span>', "ok");
  }

  // -----------------------------------------------------------------------
  // wiring
  // -----------------------------------------------------------------------

  function ready() {
    el("hive-signin").addEventListener("click", signIn);
    el("hive-code-send").addEventListener("click", sendCode);
    el("hive_code_input").addEventListener("keydown", function (event) {
      if (event.key === "Enter") { sendCode(); }
    });
    el("octopus-look").addEventListener("click", lookUpOctopus);
    el("place-look").addEventListener("click", lookUpPlace);
    el("numbers-save").addEventListener("click", saveNumbers);
    el("display-save").addEventListener("click", saveDisplay);
    el("refresh-now").addEventListener("click", refreshNow);
    var pruneButton = el("history-prune");
    if (pruneButton) { pruneButton.addEventListener("click", pruneHistory); }
    el("forget").addEventListener("click", forget);

    // Every field is remembered as it is typed, so a reload does not
    // throw the work away.
    document.addEventListener("input", remember);
    document.addEventListener("change", remember);

    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ready);
  } else {
    ready();
  }

  window.emberSettings = { readStore: readStore, forgetStore: forgetStore };
})();
