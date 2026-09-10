/* Spark Cockpit — mobile companion script for opencode's web interface.
   Served by the agent relay, injected into the served index.html before
   opencode's module (so no CSP inline-script is needed and the theme choice
   lands before the app paints). opencode's own code is never modified.

   First, and for EVERY device on every load: the secure-context polyfills.
   The relay serves the app over plain HTTP on a tailnet address, and HTTP
   off the tailnet is not a secure context, so Safari and Chrome hide
   crypto.subtle and navigator.clipboard entirely. opencode hashes every
   attachment with crypto.subtle.digest("SHA-256", ...) the moment a photo is
   picked (TypeError: reading 'digest', the file silently never reaches the
   prompt) and copies with navigator.clipboard.writeText. On this box
   (http://127.0.0.1:4096) both exist, which is why it works there and not on
   a phone. A classic script runs before any module script regardless of
   position, so these are in place before opencode's module evaluates.

   Then, for touch devices: a dark theme default for coarse pointers, a
   "sur le serveur" session card on the home (a fresh browser's home lists no
   existing sessions: projects and session lists are per-browser local state,
   so the natural way back from a new phone was a dead end), and lifting the
   composer above the on-screen keyboard (iOS does not resize an iframe for
   the keyboard, and opencode pins its dock to the layout viewport bottom). */
(function () {
  "use strict";

  // ── the secure-context polyfills (every device, HTTP relay) ──────────────
  // SHA-256 in plain JS: the only WebCrypto the app uses is digest("SHA-256")
  // (verified: two call sites, no other crypto.subtle method anywhere), so a
  // faithful digest is a complete substitute; it is used for attachment
  // identity (dedup/cache keys), never as a security boundary.
  function sha256(bytes) {
    var K = [
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
      0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
      0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
      0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
      0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
      0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
    ];
    var H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    var len = bytes.length;
    var bitLen = len * 8;
    var withPad = new Uint8Array((((len + 8) >> 6) + 1) * 64);
    withPad.set(bytes);
    withPad[len] = 0x80;
    new DataView(withPad.buffer).setUint32(withPad.length - 4, bitLen >>> 0);
    new DataView(withPad.buffer).setUint32(withPad.length - 8, Math.floor(bitLen / 4294967296));
    var w = new Uint32Array(64);
    for (var off = 0; off < withPad.length; off += 64) {
      var v = new DataView(withPad.buffer, withPad.byteOffset + off, 64);
      for (var i = 0; i < 16; i++) w[i] = v.getUint32(i * 4);
      for (i = 16; i < 64; i++) {
        var s0 = ((w[i-15] >>> 7) | (w[i-15] << 25)) ^ ((w[i-15] >>> 18) | (w[i-15] << 14)) ^ (w[i-15] >>> 3);
        var s1 = ((w[i-2] >>> 17) | (w[i-2] << 15)) ^ ((w[i-2] >>> 19) | (w[i-2] << 13)) ^ (w[i-2] >>> 10);
        w[i] = (w[i-16] + s0 + w[i-7] + s1) >>> 0;
      }
      var a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
      for (i = 0; i < 64; i++) {
        var S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        var ch = (e & f) ^ (~e & g);
        var t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
        var S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        var maj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + maj) >>> 0;
        h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0; H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
      H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0; H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
    }
    var out = new Uint8Array(32);
    var ov = new DataView(out.buffer);
    for (i = 0; i < 8; i++) ov.setUint32(i * 4, H[i]);
    return out;
  }

  function subtleDigest(algorithm, data) {
    var bytes;
    if (data instanceof ArrayBuffer) bytes = new Uint8Array(data);
    else if (data && data.buffer instanceof ArrayBuffer) bytes = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    else return Promise.reject(new TypeError("digest: unsupported data"));
    var name = typeof algorithm === "string" ? algorithm : (algorithm && algorithm.name);
    if (String(name).toUpperCase() !== "SHA-256") return Promise.reject(new Error("polyfill digests SHA-256 only, asked for " + name));
    return Promise.resolve(sha256(bytes).buffer);
  }

  // navigator.clipboard.writeText through the legacy copy path, which iOS
  // still honours inside a user gesture (a copy button always is one).
  function copyText(text) {
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement("textarea");
        ta.value = text == null ? "" : String(text);
        ta.setAttribute("readonly", "");
        ta.style.cssText = "position:fixed;top:0;left:-9999px;opacity:0";
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, ta.value.length);
        var ok = document.execCommand("copy");
        ta.remove();
        ok ? resolve() : reject(new Error("copy blocked"));
      } catch (e) { reject(e); }
    });
  }

  try {
    if (window.isSecureContext === false) {
      if (window.crypto && !window.crypto.subtle) window.crypto.subtle = { digest: subtleDigest };
      if (navigator && !navigator.clipboard)
        Object.defineProperty(navigator, "clipboard", { value: { writeText: copyText }, configurable: true });
    }
  } catch (e) { /* a host that refuses the shim keeps whatever it has */ }

  var coarse = window.matchMedia("(pointer: coarse)");

  // Dark by default for touch, exactly the keys the app's own preload reads.
  try {
    if (coarse.matches && localStorage.getItem("opencode-color-scheme") === null) {
      localStorage.setItem("opencode-color-scheme", "dark");
      if (document.documentElement.dataset.colorScheme === "light") {
        document.documentElement.dataset.colorScheme = "dark";
        document.documentElement.style.backgroundColor = "#080808";
        var meta = document.querySelector("meta[name='theme-color']");
        if (meta) meta.setAttribute("content", "#080808");
      }
    }
  } catch (e) { /* private mode: the rest still works */ }

  if (!coarse.matches) return;

  // The app routes a session as /server/<base64 of the server URL, padding
  // stripped>/session/<id>; served same-origin, that server URL is this page's
  // own origin. The padding matters: a key ending in = renders the app's own
  // "Invalid server route" crash screen (verified against 1.18.27).
  function sessionHref(id) {
    var key = null;
    try { key = window.btoa(window.location.origin).replace(/=+$/, ""); } catch (e) {}
    return key ? "/server/" + key + "/session/" + encodeURIComponent(id) : null;
  }

  var KEY = "spark.lastSession";
  var isSession = function () { return window.location.pathname.indexOf("/session/") !== -1; };

  function remember() {
    try { if (isSession()) localStorage.setItem(KEY, window.location.pathname); } catch (e) {}
  }

  // ── the home's session card ───────────────────────────────────────────────
  // The empty home of a fresh browser lists nothing (no project was added in
  // THIS browser), so ask the server directly. The route is the same API the
  // app itself calls, on this same origin, behind the cockpit session that is
  // already on the wire. Clicking a row is a plain link: the relay serves the
  // app for /server/... paths, so the SPA boots straight into that session.
  var CARD_ID = "spark-sessions";
  var cached = null;

  function dark() {
    var s = null;
    try { s = localStorage.getItem("opencode-color-scheme"); } catch (e) {}
    return s !== "light";
  }

  function cardStyle() {
    var d = dark();
    return [
      "position:fixed", "left:12px", "right:12px",
      "bottom:calc(12px + env(safe-area-inset-bottom, 0px))",
      "z-index:2147483000", "max-height:46vh", "overflow-y:auto",
      "border-radius:16px", "padding:6px 0 8px",
      "background:" + (d ? "rgba(24,24,24,.96)" : "rgba(255,255,255,.97)"),
      "border:1px solid " + (d ? "rgba(255,255,255,.10)" : "rgba(0,0,0,.10)"),
      "box-shadow:0 8px 28px rgba(0,0,0,.35)",
      "-webkit-backdrop-filter:blur(8px)", "backdrop-filter:blur(8px)",
      "font:440 14px/1.35 system-ui,sans-serif",
      "color:" + (d ? "#eaeaea" : "#181818"),
      "touch-action:manipulation"
    ].join(";");
  }

  function rowStyle() {
    var d = dark();
    return [
      "display:block", "padding:10px 14px", "text-decoration:none",
      "color:" + (d ? "#eaeaea" : "#181818")
    ].join(";");
  }

  function fetchSessions(done) {
    fetch("/api/session?limit=8&order=desc", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        cached = (j && (Array.isArray(j) ? j : j.data)) || [];
        done();
      })
      .catch(function () { done(); });
  }

  function renderCard() {
    var root = document.getElementById("root");
    var onHome = window.location.pathname === "/" && !isSession();
    var old = document.getElementById(CARD_ID);
    if (!onHome) { if (old) old.remove(); return; }
    if (!root || !root.childElementCount) return;
    if (!cached) { fetchSessions(function () { renderCard(); }); return; }
    // When THIS browser has its own list up (a project was added here), the
    // app already shows the sessions; a second list would only duplicate it.
    if (root.querySelector("[data-component=session-card], [data-component=session-item], [data-component=session-row], [data-component=home-session-row]")) {
      if (old) old.remove();
      return;
    }
    var items = cached.slice().sort(function (a, b) {
      return ((b.time && b.time.updated) || 0) - ((a.time && a.time.updated) || 0);
    }).slice(0, 6);
    var saved = null;
    try { saved = localStorage.getItem(KEY); } catch (e) {}
    if (!items.length && !saved) { if (old) old.remove(); return; }
    var d = dark();
    if (!old) {
      old = document.createElement("div");
      old.id = CARD_ID;
      old.setAttribute("style", cardStyle());
      (root).appendChild(old);
    } else if (old.parentElement !== root) {
      root.appendChild(old);
    }
    var head = '<div data-spark-refresh="1" style="padding:4px 14px 8px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:' + (d ? "#8b8b8b" : "#6b6b6b") + '">Sur le serveur</div>';
    var rows = items.map(function (s) {
      var href = sessionHref(s.id);
      if (!href) return "";
      var title = (s.title || s.id).replace(/[<>&]/g, "");
      var dir = (s.directory || "").replace(/[<>&]/g, "");
      var fresh = saved && href === saved ? " · <b>reprendre</b>" : "";
      return '<a href="' + href + '" style="' + rowStyle() + '">'
        + '<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + title + fresh + "</div>"
        + (dir ? '<div style="font-size:11px;color:' + (d ? "#8b8b8b" : "#6b6b6b") + ';white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + dir + "</div>" : "")
        + "</a>";
    }).join("");
    old.innerHTML = head + rows;
  }

  // ── the keyboard ──────────────────────────────────────────────────────────
  // iOS never resizes an iframe for the keyboard: the dock stays at the layout
  // bottom and the keyboard covers it. visualViewport is the only honest
  // measure of what the keyboard left; translate the dock up by exactly that.
  // (Android with interactive-widget=resizes-content shrinks the layout itself,
  // kb then measures ~0, and this stays out of the way.)
  function lift() {
    var dock = document.querySelector("[data-component=session-prompt-dock]");
    if (!dock) return;
    var vv = window.visualViewport;
    var kb = vv ? Math.max(0, window.innerHeight - vv.height - vv.offsetTop) : 0;
    var liftPx = kb > 60 ? kb : 0;
    var want = liftPx ? "translateY(-" + liftPx + "px)" : "";
    if (dock.style.transform !== want) dock.style.transform = want;
  }

  renderCard();
  document.addEventListener("click", function (e) {
    if (e.target && e.target.closest && e.target.closest("[data-spark-refresh]")) { cached = null; renderCard(); }
    setTimeout(function () { remember(); renderCard(); }, 350);
  }, true);
  window.addEventListener("popstate", function () { remember(); renderCard(); });
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", lift);
    window.visualViewport.addEventListener("scroll", lift);
  }
  setInterval(function () { remember(); renderCard(); if (isSession()) lift(); }, 500);
})();
