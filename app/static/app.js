"use strict";

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);

const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const c of children) if (c != null) node.append(c);
  return node;
};

const svgIcon = (path) => {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(ns, "path");
  p.setAttribute("d", path);
  p.setAttribute("fill", "none");
  p.setAttribute("stroke", "currentColor");
  p.setAttribute("stroke-width", "2");
  p.setAttribute("stroke-linecap", "round");
  p.setAttribute("stroke-linejoin", "round");
  svg.append(p);
  return svg;
};

const ICONS = {
  video: "M4 6.5A1.5 1.5 0 0 1 5.5 5h9A1.5 1.5 0 0 1 16 6.5v11a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 4 17.5v-11ZM16 10l4-2.5v9L16 14",
  audio: "M9 18V6l11-2v12M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm11-2a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
  redo: "M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5M5 20h14",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3",
  subtitle: "M3 6.5A1.5 1.5 0 0 1 4.5 5h15A1.5 1.5 0 0 1 21 6.5v11a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5v-11ZM7 12h3M13 12h4M7 15.5h6M15.5 15.5H17",
};

function humanSize(n) {
  if (!n) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i < 2 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function humanDuration(sec) {
  if (sec == null || !isFinite(sec)) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return `${h ? h + ":" : ""}${mm}:${String(s).padStart(2, "0")}`;
}

function timeAgo(iso) {
  if (!iso) return "";
  const parsed = new Date(iso).getTime();
  if (isNaN(parsed)) return "";
  const diff = (Date.now() - parsed) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

const thumbUrl = (u) => (u ? `/api/thumb?u=${encodeURIComponent(u)}` : "");

async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
  } catch {
    throw new Error("Can't reach the server. Is it still running?");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.message;
    if (!msg && Array.isArray(data.detail)) msg = "That doesn't look like a valid link.";
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return data;
}

// ---------------------------------------------------------------- state
const state = {
  info: null,        // current MediaInfo
  jobId: null,       // active job
  source: null,      // EventSource for the active job
  busy: false,      // a download is in progress (UI runs one at a time)
};

// ---------------------------------------------------------------- errors
function showError(msg) {
  $("error-text").textContent = msg;
  $("error").hidden = false;
}
const clearError = () => { $("error").hidden = true; };

// ---------------------------------------------------------------- info
async function fetchInfo(url) {
  clearError();
  const go = $("go-btn");
  go.classList.add("loading");
  go.disabled = true;
  try {
    const info = await api("/api/info", { method: "POST", body: JSON.stringify({ url }) });
    state.info = info;
    renderPreview(info);
    renderPlaylist(info);
    renderSubtitles(info);
    renderOptions(info);
    updateSubtitleOption();
  } catch (e) {
    showError(e.message);
  } finally {
    go.classList.remove("loading");
    go.disabled = false;
  }
}

function renderPreview(info) {
  const img = $("thumb");
  const thumbBox = img.parentElement;
  thumbBox.classList.remove("portrait");
  img.onload = () => thumbBox.classList.toggle("portrait", img.naturalHeight > img.naturalWidth);
  img.onerror = () => { img.removeAttribute("src"); };
  if (info.thumbnail) img.src = thumbUrl(info.thumbnail); else img.removeAttribute("src");

  const isList = info.kind === "playlist";
  const unit = info.platform === "spotify" ? "tracks" : "videos";
  const badge = isList ? `${info.entries.length} ${unit}` : humanDuration(info.duration);
  $("duration").textContent = badge;
  $("duration").hidden = !badge;
  const site = { youtube: "YouTube", instagram: "Instagram", spotify: "Spotify", twitter: "Twitter/X" }[info.platform] || info.platform;
  const listKind = { youtube: "playlist", instagram: "carousel", spotify: info.album ? "album" : "playlist", twitter: "post" }[info.platform];
  $("platform").textContent = isList ? `${site} ${listKind}` : site;
  // Spotify: show which YouTube upload the audio comes from, so a wrong match is easy to spot.
  const match = info.match;
  $("match").hidden = !match;
  if (match) {
    const link = $("match-link");
    link.href = match.url;
    link.textContent = [match.title, match.channel].filter(Boolean).join(" · ");
  }
  $("title").textContent = info.title;
  $("uploader").textContent = info.uploader || "";
  const open = $("open-playlist");
  open.hidden = !info.playlist_url;
  open.onclick = () => {
    $("url").value = info.playlist_url;
    fetchInfo(info.playlist_url);
  };
  $("preview").hidden = false;
}

// ---------------------------------------------------------------- playlist
function selectedItems() {
  return [...$("playlist-list").querySelectorAll("input:checked")].map((c) => Number(c.value));
}

function updatePlaylistCount() {
  const total = state.info?.entries.length || 0;
  const n = selectedItems().length;
  $("playlist-count").textContent = `${n} of ${total} selected`;
  for (const li of $("playlist-list").children) li.classList.toggle("off", !li.querySelector("input").checked);
  if (!state.busy) {
    for (const b of $("options").querySelectorAll(".option")) b.disabled = n === 0;
  }
}

function renderPlaylist(info) {
  const box = $("playlist");
  const list = $("playlist-list");
  if (info.kind !== "playlist") {
    box.hidden = true;
    list.replaceChildren();
    return;
  }
  list.replaceChildren(
    ...info.entries.map((e) => {
      const box = el("input", { type: "checkbox", checked: true, value: String(e.index) });
      box.addEventListener("change", updatePlaylistCount);
      const img = el("img", { alt: "", loading: "lazy", referrerPolicy: "no-referrer" });
      img.onerror = () => img.remove();
      if (e.thumbnail) img.src = thumbUrl(e.thumbnail);
      return el(
        "li",
        {},
        el(
          "label",
          {},
          box,
          el("span", { className: "p-thumb" }, img),
          el("span", { className: "p-title" }, e.title, e.artist ? el("span", { className: "p-artist", textContent: ` · ${e.artist}` }) : null),
          el("span", { className: "p-dur", textContent: humanDuration(e.duration) }),
        ),
      );
    }),
  );
  box.hidden = false;
  updatePlaylistCount();
}

function setAllItems(checked) {
  for (const c of $("playlist-list").querySelectorAll("input")) c.checked = checked;
  updatePlaylistCount();
}
$("select-all").addEventListener("click", () => setAllItems(true));
$("select-none").addEventListener("click", () => setAllItems(false));

// ---------------------------------------------------------------- subtitles
function renderSubtitles(info) {
  const row = $("subs-row");
  const select = $("subs");
  const tracks = info.subtitles || [];
  select.replaceChildren(
    el("option", { value: "", textContent: "None" }),
    ...tracks.map((t) => el("option", { value: t.id, textContent: t.name })),
  );
  row.hidden = tracks.length === 0;
}

// The "Subtitles" option saves the picked track, or the first (English-first) one.
function updateSubtitleOption() {
  const detail = $("options").querySelector('.option[data-id="srt"] .detail');
  const tracks = state.info?.subtitles || [];
  if (!detail || !tracks.length) return;
  const picked = tracks.find((t) => t.id === $("subs").value) || tracks[0];
  detail.textContent = `SRT file · ${picked.name}`;
}
$("subs").addEventListener("change", updateSubtitleOption);

function renderOptions(info) {
  const box = $("options");
  box.replaceChildren();
  for (const opt of info.options) {
    const size = opt.filesize ? `~${humanSize(opt.filesize)}` : "";
    const iconPath = ICONS[opt.kind] || ICONS.video;
    const btn = el(
      "button",
      { type: "button", className: `option ${opt.kind}` },
      el("span", { className: "icon" }, svgIcon(iconPath)),
      el("span", { className: "label", textContent: opt.label }),
      el("span", { className: "detail", textContent: opt.detail }),
      el("span", { className: "size", textContent: size }),
    );
    btn.dataset.id = opt.id;
    btn.disabled = state.busy || (info.kind === "playlist" && selectedItems().length === 0);
    btn.addEventListener("click", () => startDownload(info.url, opt.id, btn));
    box.append(btn);
  }
  box.hidden = info.options.length === 0;
  if (info.platform === "instagram" && !info.options.some((o) => o.kind === "audio")) {
    box.append(el("p", { className: "muted hint", textContent: "No audio track was available for this post. Instagram sometimes hides audio from logged-out requests — configuring cookies can help." }));
  }
}

function setOptionsDisabled(disabled) {
  for (const b of $("options").querySelectorAll(".option")) {
    b.disabled = disabled;
    if (!disabled) b.classList.remove("active");
  }
}

// ---------------------------------------------------------------- download
async function startDownload(url, optionId, button) {
  clearError();
  if (state.busy) return;
  state.busy = true;
  setOptionsDisabled(true);
  if (button) button.classList.add("active");
  resetProgress();
  $("progress").hidden = false;
  $("progress").scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const body = { url, option_id: optionId };
    const info = state.info && state.info.url === url ? state.info : null;
    if (info?.kind === "playlist") body.items = selectedItems();
    const sub = $("subs").value;
    if (info && sub && !$("subs-row").hidden) body.subtitle = sub;
    const { job_id } = await api("/api/download", { method: "POST", body: JSON.stringify(body) });
    state.jobId = job_id;
    listen(job_id);
  } catch (e) {
    finishProgress("failed", e.message);
  }
}

const STAGE_TEXT = {
  queued: "Waiting in queue…",
  downloading: "Downloading",
  merging: "Merging video and audio…",
  converting: "Converting to MP3…",
  processing: "Finishing up…",
  done: "Done",
  cancelled: "Cancelled",
  error: "Failed",
};

function resetProgress() {
  const p = $("progress");
  p.classList.remove("done", "failed");
  $("stage").textContent = "Starting…";
  $("percent").textContent = "";
  $("speed").textContent = "";
  $("eta").textContent = "";
  $("bar-fill").style.width = "0%";
  $("save-btn").hidden = true;
  $("result-files").hidden = true;
  $("result-files").replaceChildren();
  $("cancel-btn").hidden = false;
  $("cancel-btn").disabled = false;
  document.querySelector(".bar").classList.add("indeterminate");
}

function listen(jobId) {
  const src = new EventSource(`/api/progress/${jobId}`);
  state.source = src;
  src.onmessage = (msg) => {
    try {
      const ev = JSON.parse(msg.data);
      renderProgress(ev);
      if (ev.stage === "done") {
        closeSource();
        onDone(jobId, ev);
      } else if (ev.stage === "error" || ev.stage === "cancelled") {
        closeSource();
        finishProgress("failed", ev.stage === "error" ? ev.message : null);
      }
    } catch (err) {
      console.warn("Malformed progress event:", err);
    }
  };
  src.onerror = async () => {
    // EventSource reconnects by itself; stop if the job is gone (e.g. server restarted).
    try {
      await api(`/api/jobs/${jobId}`);
    } catch (e) {
      closeSource();
      finishProgress("failed", e.message);
    }
  };
}

function closeSource() {
  if (state.source) state.source.close();
  state.source = null;
}

function renderProgress(ev) {
  const bar = document.querySelector(".bar");
  // Playlist jobs put "2/5 · Title" in the message; single downloads use it
  // for steps like "Reading video info…" or "Writing title and tags…".
  const label = ev.message || STAGE_TEXT[ev.stage] || ev.stage;
  $("stage").textContent = label;
  $("stage").title = label;
  // 0% = not started yet, 100% = post-processing (merging etc.): animate instead.
  const known = ev.percent != null && ev.percent > 0 && ev.percent < 100;
  bar.classList.toggle("indeterminate", !known && ev.stage !== "done");
  if (ev.percent != null) $("bar-fill").style.width = `${ev.percent}%`;
  bar.setAttribute("aria-valuenow", String(Math.round(ev.percent || 0)));
  $("percent").textContent = known || ev.stage === "done" ? `${Math.floor(ev.percent)}%` : "";
  $("speed").textContent = ev.stage === "downloading" && ev.speed ? `${humanSize(ev.speed)}/s` : "";
  $("eta").textContent = ev.stage === "downloading" && ev.eta ? `${humanDuration(ev.eta)} left` : "";
}

function onDone(jobId, ev) {
  const p = $("progress");
  p.classList.add("done");
  const many = (ev.files || []).length > 1;
  $("stage").textContent = [ev.files ? ev.message : "Done", humanSize(ev.total_bytes)].filter(Boolean).join(" · ");
  $("speed").textContent = ev.filename || "";
  $("eta").textContent = "";
  $("cancel-btn").hidden = true;
  const save = $("save-btn");
  save.href = `/api/file/${jobId}`;
  save.setAttribute("download", ev.filename || "");
  save.textContent = many ? `Save all (${ev.files.length}) as ZIP` : "Save file";
  save.hidden = false;
  if (many) {
    // Individual files too — handier on phones than a ZIP.
    $("result-files").replaceChildren(
      ...ev.files.map((name, i) => {
        const a = el("a", { href: `/api/file/${jobId}/${i + 1}`, textContent: name, title: name });
        a.setAttribute("download", name);
        return el("li", {}, a);
      }),
    );
    $("result-files").hidden = false;
  }
  state.jobId = null;
  state.busy = false;
  setOptionsDisabled(false);
  if (state.info?.kind === "playlist") updatePlaylistCount();
  save.click(); // start the browser download right away
  loadHistory();
}

function finishProgress(kind, message) {
  const p = $("progress");
  p.classList.add(kind);
  document.querySelector(".bar").classList.remove("indeterminate");
  $("cancel-btn").hidden = true;
  $("speed").textContent = "";
  $("eta").textContent = "";
  if (message) showError(message);
  if (!message) $("stage").textContent = "Cancelled";
  state.jobId = null;
  state.busy = false;
  setOptionsDisabled(false);
  if (state.info?.kind === "playlist") updatePlaylistCount();
}

$("cancel-btn").addEventListener("click", async () => {
  if (!state.jobId) return;
  $("cancel-btn").disabled = true;
  try { await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" }); } catch { /* final event arrives via SSE */ }
});

// ---------------------------------------------------------------- history
async function loadHistory() {
  let items = [];
  try { items = await api("/api/history"); } catch { return; }
  const list = $("history-list");
  list.replaceChildren(...items.map(historyRow));
  $("history-empty").hidden = items.length > 0;
  $("history-count").textContent = items.length ? `${items.length}` : "";
}

function historyRow(item) {
  const img = el("img", { alt: "", loading: "lazy", referrerPolicy: "no-referrer" });
  img.onerror = () => img.remove();
  if (item.thumbnail) img.src = thumbUrl(item.thumbnail);

  const title = el("button", { type: "button", className: "h-title", textContent: item.title, title: "Show options for this video" });
  title.addEventListener("click", () => {
    $("url").value = item.url;
    window.scrollTo({ top: 0, behavior: "smooth" });
    fetchInfo(item.url);
  });

  const what = item.item_count ? `${item.item_count} files` : item.ext.toUpperCase();
  const sub = [item.option_label, what, humanSize(item.filesize), timeAgo(item.created_at)].filter(Boolean).join(" · ");

  const redo = el("button", { type: "button", className: "icon-btn", title: "Download again", ariaLabel: "Download again" }, svgIcon(ICONS.redo));
  redo.addEventListener("click", () => {
    if (state.busy) return showError("Wait for the current download to finish.");
    window.scrollTo({ top: 0, behavior: "smooth" });
    $("url").value = item.url;
    if (item.item_count) {
      fetchInfo(item.url); // playlists: pick the items again
      return;
    }
    $("preview").hidden = true;
    $("playlist").hidden = true;
    $("subs-row").hidden = true;
    $("options").hidden = true;
    startDownload(item.url, item.option_id, null);
    fetchInfo(item.url);
  });

  const del = el("button", { type: "button", className: "icon-btn danger", title: "Remove from history", ariaLabel: "Remove from history" }, svgIcon(ICONS.trash));
  del.addEventListener("click", async () => {
    try {
      await api(`/api/history/${item.id}`, { method: "DELETE" });
      loadHistory();
    } catch (e) { showError(e.message); }
  });

  return el(
    "li",
    { className: "history-item" },
    el("div", { className: "h-thumb" }, img),
    el("div", { className: "h-body" }, title, el("div", { className: "h-sub", textContent: sub })),
    redo,
    del,
  );
}

// ---------------------------------------------------------------- health
async function loadHealth() {
  try {
    const h = await api("/api/health");
    const dot = $("health");
    dot.classList.add(h.ffmpeg_ok ? "ok" : "bad");
    dot.title = h.ffmpeg_ok ? "Ready" : "ffmpeg missing";
    $("footer").textContent = `yt-dlp ${h.yt_dlp} · ffmpeg ${h.ffmpeg || "missing"}`;
    const banner = $("banner");
    if (!h.ffmpeg_ok) {
      banner.replaceChildren("ffmpeg isn't installed on the server, so downloads will fail. See the README for install steps.");
      banner.hidden = false;
    } else if (h.yt_dlp_update_available) {
      banner.replaceChildren(
        `A newer yt-dlp (${h.yt_dlp_latest}) is available. If downloads start failing, update with `,
        el("code", { textContent: "pip install -U yt-dlp" }),
        " and restart.",
      );
      banner.hidden = false;
    }
  } catch {
    $("health").classList.add("bad");
  }
}

// ---------------------------------------------------------------- input
const SUPPORTED = /(^|\.)(youtube\.com|youtu\.be|instagram\.com|open\.spotify\.com|twitter\.com|x\.com)\//i;

$("url-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const url = $("url").value.trim();
  if (url) fetchInfo(url);
});

// Fetch automatically when a supported link is pasted.
$("url").addEventListener("paste", () => {
  setTimeout(() => {
    const url = $("url").value.trim();
    if (SUPPORTED.test(url)) fetchInfo(url);
  }, 0);
});

// The Clipboard API only exists in secure contexts (https or localhost).
if (navigator.clipboard && navigator.clipboard.readText) {
  const paste = $("paste-btn");
  paste.hidden = false;
  paste.addEventListener("click", async () => {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (!text) return;
      $("url").value = text;
      fetchInfo(text);
    } catch {
      $("url").focus();
    }
  });
}

// Support share targets / bookmarklets: /?url=https://...
const shared = new URLSearchParams(location.search).get("url");
if (shared) {
  $("url").value = shared;
  fetchInfo(shared);
}

loadHealth();
loadHistory();
