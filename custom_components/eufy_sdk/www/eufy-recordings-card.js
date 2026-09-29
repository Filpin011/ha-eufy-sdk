/*
 * Eufy SD-card recordings — a dashboard card in the "events grid" style.
 *
 * Camera dropdown + date picker + a grid of snapshot thumbnails for that day.
 * Everything goes through the integration: the two WebSocket commands
 * (eufy_sdk/recordings/cameras, eufy_sdk/recordings/list) and the
 * /api/eufy_sdk/recording proxy for the snapshot and the clip. Thumbnails and
 * the clip are fetched with the frontend's auth (fetchWithAuth), so no signed
 * URLs are needed and it works the same on an HTTPS install.
 *
 * Usage in a dashboard (auto-registered by the integration):
 *   type: custom:eufy-recordings-card
 *   camera: T8171...      # optional: default selected camera serial
 *   title: Registrazioni  # optional
 */

const CARD_TAG = "eufy-recordings-card";

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;
const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const b64 = (s) => btoa(unescape(encodeURIComponent(s)));

// "20260929065745" or "2026-09-29 06:57:45" → "06:57:45"
function timeLabel(v) {
  const s = String(v ?? "");
  const m = s.match(/(\d{2}):(\d{2}):(\d{2})/);
  if (m) return `${m[1]}:${m[2]}:${m[3]}`;
  const d = s.match(/(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/);
  return d ? `${d[4]}:${d[5]}:${d[6]}` : s;
}

class EufyRecordingsCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._selectedCamera = this._config.camera || null;
    this._date = new Date();
    this._built = false;
    this._thumbUrls = [];
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    if (!this._camerasLoaded) this._loadCameras();
  }

  getCardSize() {
    return 8;
  }

  // Authenticated fetch that works whether or not hass exposes fetchWithAuth: fall back to the
  // current access token as a Bearer header (the frontend keeps it fresh in hass.auth.data).
  async _authFetch(url) {
    const h = this._hass;
    if (h && typeof h.fetchWithAuth === "function") return h.fetchWithAuth(url);
    const token =
      h?.auth?.data?.access_token ??
      h?.connection?.options?.auth?.data?.access_token ??
      null;
    return fetch(url, token ? { headers: { authorization: `Bearer ${token}` } } : { credentials: "same-origin" });
  }

  // ── DOM ──────────────────────────────────────────────────────────────────
  _build() {
    this._built = true;
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 12px 12px 16px; }
        .bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
        .title { font-size: 1.15rem; font-weight: 600; margin-right: auto; }
        select, input[type=date], button {
          font: inherit; padding: 6px 8px; border-radius: 8px;
          border: 1px solid var(--divider-color, #444);
          background: var(--card-background-color, #1c1c1c);
          color: var(--primary-text-color, #fff);
        }
        button { cursor: pointer; }
        button:hover { background: var(--secondary-background-color, #2a2a2a); }
        .status { padding: 24px 8px; text-align: center; color: var(--secondary-text-color, #9aa); }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
        .clip { position: relative; border-radius: 10px; overflow: hidden; cursor: pointer;
                background: var(--secondary-background-color, #2a2a2a); aspect-ratio: 16/9; }
        .clip img { width: 100%; height: 100%; object-fit: cover; display: block; background: #111; }
        .clip .ph { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center;
                    color: var(--secondary-text-color, #9aa); font-size: 0.8rem; }
        .clip .cap { position: absolute; left: 0; right: 0; bottom: 0; padding: 4px 6px;
                     font-size: 0.75rem; color: #fff; background: linear-gradient(transparent, rgba(0,0,0,.75)); }
        .clip .play { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
                      opacity: 0; transition: opacity .15s; }
        .clip:hover .play { opacity: 1; }
        .clip .play svg { width: 42px; height: 42px; filter: drop-shadow(0 1px 3px rgba(0,0,0,.6)); }
        .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.85); display: flex;
                   align-items: center; justify-content: center; z-index: 9999; }
        .overlay .box { max-width: 92vw; max-height: 92vh; display: flex; flex-direction: column; gap: 8px; }
        .overlay video { max-width: 92vw; max-height: 80vh; border-radius: 8px; background: #000; }
        .overlay .msg { color: #fff; text-align: center; min-height: 1.2em; }
        .overlay .close { align-self: flex-end; }
      </style>
      <ha-card>
        <div class="bar">
          <div class="title"></div>
          <select class="cam"></select>
          <button class="prev" title="Giorno precedente">‹</button>
          <input class="date" type="date" />
          <button class="next" title="Giorno successivo">›</button>
          <button class="reload" title="Ricarica">⟳</button>
        </div>
        <div class="status"></div>
        <div class="grid"></div>
      </ha-card>`;

    this.$ = {
      title: root.querySelector(".title"),
      cam: root.querySelector(".cam"),
      date: root.querySelector(".date"),
      prev: root.querySelector(".prev"),
      next: root.querySelector(".next"),
      reload: root.querySelector(".reload"),
      status: root.querySelector(".status"),
      grid: root.querySelector(".grid"),
    };
    this.$.title.textContent = this._config.title || "Registrazioni Eufy";
    this.$.date.value = isoDate(this._date);
    this.$.cam.addEventListener("change", () => {
      this._selectedCamera = this.$.cam.value;
      this._loadDay();
    });
    this.$.date.addEventListener("change", () => {
      const [y, m, d] = this.$.date.value.split("-").map(Number);
      this._date = new Date(y, m - 1, d);
      this._loadDay();
    });
    this.$.prev.addEventListener("click", () => this._shiftDay(-1));
    this.$.next.addEventListener("click", () => this._shiftDay(1));
    this.$.reload.addEventListener("click", () => this._loadDay(true));
  }

  _shiftDay(delta) {
    this._date = new Date(this._date.getTime() + delta * 86400000);
    this.$.date.value = isoDate(this._date);
    this._loadDay();
  }

  // ── data ─────────────────────────────────────────────────────────────────
  async _loadCameras() {
    this._camerasLoaded = true;
    try {
      const { cameras } = await this._hass.callWS({ type: "eufy_sdk/recordings/cameras" });
      this.$.cam.innerHTML = "";
      for (const c of cameras || []) {
        const o = document.createElement("option");
        o.value = c.sn;
        o.textContent = c.name || c.sn;
        this.$.cam.appendChild(o);
      }
      if (!this._selectedCamera && cameras && cameras.length) this._selectedCamera = cameras[0].sn;
      if (this._selectedCamera) this.$.cam.value = this._selectedCamera;
      this._loadDay();
    } catch (e) {
      this._camerasLoaded = false;
      this._setStatus(`Impossibile leggere le camere: ${e.message || e}`);
    }
  }

  _setStatus(text) {
    this.$.status.textContent = text || "";
    this.$.status.style.display = text ? "block" : "none";
  }

  _revokeThumbs() {
    for (const u of this._thumbUrls) URL.revokeObjectURL(u);
    this._thumbUrls = [];
  }

  async _loadDay(force = false) {
    if (!this._selectedCamera) return;
    const sn = this._selectedCamera;
    const day = ymd(this._date);
    const key = `${sn}|${day}`;
    this._cache = this._cache || {};
    if (force) delete this._cache[key];
    // Already have it → show instantly, no bridge round-trip (a battery camera is slow to wake).
    if (this._cache[key]) {
      this._render(sn, day, this._cache[key]);
      return;
    }
    // One list at a time: while a slow query runs, just remember that the view moved and reload once
    // it returns — never pile up 40s queries behind each other.
    if (this._loading) {
      this._dirty = true;
      return;
    }
    this._loading = true;
    this._dirty = false;
    this._revokeThumbs();
    this.$.grid.innerHTML = "";
    this._setStatus("Caricamento… una camera a batteria deve svegliarsi, può richiedere fino a un minuto.");
    let rows = null;
    let err = null;
    try {
      const res = await this._hass.callWS({ type: "eufy_sdk/recordings/list", sn, date: day });
      rows = res.recordings || [];
    } catch (e) {
      err = e;
    }
    this._loading = false;
    // View moved while we waited → load the current view now, discard this stale result.
    if (this._dirty || this._selectedCamera !== sn || ymd(this._date) !== day) {
      this._dirty = false;
      this._loadDay();
      return;
    }
    if (err) {
      this._setStatus(`Errore: ${err.message || err}`);
      return;
    }
    if (!rows.length) {
      this._setStatus("Nessuna registrazione per questo giorno. Se sai che ci sono, la camera non ha risposto: tieni aperta la vista live e premi ⟳.");
      return;
    }
    this._cache[key] = rows;
    this._render(sn, day, rows);
  }

  _render(sn, day, rows) {
    if (this._selectedCamera !== sn || ymd(this._date) !== day) return;
    this._revokeThumbs();
    this.$.grid.innerHTML = "";
    this._setStatus("");
    for (const r of rows) this._renderClip(sn, r);
  }

  _renderClip(sn, row) {
    const path = row.storage_path;
    if (!path) return;
    const el = document.createElement("div");
    el.className = "clip";
    el.innerHTML = `
      <div class="ph">…</div>
      <div class="play"><svg viewBox="0 0 24 24" fill="#fff"><path d="M8 5v14l11-7z"/></svg></div>
      <div class="cap">${timeLabel(row.start_time)}</div>`;
    el.addEventListener("click", () => this._openClip(sn, row));
    this.$.grid.appendChild(el);
    if (row.thumb_path) this._loadThumb(sn, row.thumb_path, el);
  }

  async _loadThumb(sn, thumbPath, el) {
    try {
      const url = `/api/eufy_sdk/recording?sn=${encodeURIComponent(sn)}&path=${encodeURIComponent(b64(thumbPath))}&kind=thumb`;
      const resp = await this._authFetch(url);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const blob = await resp.blob();
      const obj = URL.createObjectURL(blob);
      this._thumbUrls.push(obj);
      const img = document.createElement("img");
      img.src = obj;
      img.loading = "lazy";
      const ph = el.querySelector(".ph");
      if (ph) ph.replaceWith(img);
    } catch (e) {
      const ph = el.querySelector(".ph");
      if (ph) ph.textContent = "no anteprima";
    }
  }

  // ── playback overlay ───────────────────────────────────────────────────────
  async _openClip(sn, row) {
    const path = row.storage_path;
    const overlay = document.createElement("div");
    overlay.className = "overlay";
    overlay.innerHTML = `
      <div class="box">
        <button class="close">Chiudi ✕</button>
        <div class="msg">Scaricamento della clip dalla scheda SD… può richiedere fino a un minuto.</div>
        <video controls autoplay playsinline style="display:none"></video>
      </div>`;
    const close = () => {
      if (overlay._url) URL.revokeObjectURL(overlay._url);
      overlay.remove();
    };
    overlay.querySelector(".close").addEventListener("click", close);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    this.shadowRoot.appendChild(overlay);

    const msg = overlay.querySelector(".msg");
    const video = overlay.querySelector("video");
    try {
      const url = `/api/eufy_sdk/recording?sn=${encodeURIComponent(sn)}&path=${encodeURIComponent(b64(path))}`;
      const resp = await this._authFetch(url);
      if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
          const j = await resp.json();
          if (j && j.error) detail = j.error;
        } catch (_) {}
        throw new Error(detail);
      }
      const blob = await resp.blob();
      const obj = URL.createObjectURL(blob);
      overlay._url = obj;
      video.src = obj;
      video.style.display = "block";
      msg.style.display = "none";
    } catch (e) {
      msg.textContent = `Impossibile riprodurre la clip: ${e.message || e}`;
    }
  }

  disconnectedCallback() {
    this._revokeThumbs();
  }
}

if (!customElements.get(CARD_TAG)) customElements.define(CARD_TAG, EufyRecordingsCard);

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === CARD_TAG)) {
  window.customCards.push({
    type: CARD_TAG,
    name: "Eufy Recordings",
    description: "Griglia delle registrazioni SD di una camera Eufy, per giorno.",
  });
}
