(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const viewer = $("viewer");
  const grid = $("grid");
  const camBar = $("camBar");
  const overlay = $("overlay");
  const dot = $("dot");
  const stats = $("stats");
  const btnPause = $("btnPause");
  const btnSnap = $("btnSnap");
  const fpsSel = $("fpsSel");
  // 1x1 の透明 GIF。src を差し替えると進行中の MJPEG 受信が中断される
  const BLANK = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
  const POLL_MS = 2000;
  const GRID_MAX_FPS = 10; // 一覧では通信量を抑えるため、各カメラをこの fps までにする
  // HTTP/1.1 ではブラウザが同じサーバーへの同時接続を 6 本程度に制限するため、多くのカメラを MJPEG で
  // 同時に開くと詰まる。その場合は一覧の映像をスナップショットの定期取得に切り替える
  const MAX_GRID_STREAMS = 4;
  const navEntry = (performance.getEntriesByType && performance.getEntriesByType("navigation")[0]) || {};
  const multiplexed = /^h[23]/.test(navEntry.nextHopProtocol || "");

  let cameras = [];        // api/status の cameras (id, name と各カメラの状態)
  let selected = null;     // "all" (一覧) またはカメラの id
  let feeds = [];          // 表示中の映像
  let paused = false;      // ユーザー操作による一時停止
  let hiddenPause = document.hidden; // タブが非表示のため自動停止中 (裏で開かれたタブは表示されるまで繋がない)
  let serverOnline = null; // null = 未確認
  let lastStatus = null;
  let fpsCap = "";
  try { fpsCap = localStorage.getItem("kasumin.fps") || ""; } catch (e) { /* 使えない環境では無視 */ }
  fpsSel.value = fpsCap;
  if (fpsSel.value !== fpsCap) { fpsCap = ""; fpsSel.value = ""; }

  const multi = () => cameras.length > 1;
  const byId = (id) => cameras.find((c) => c.id === id);

  // ------------------------------------------------------------------ 映像 (MJPEG ストリーム / スナップショットの定期取得)

  function makeFeed(img, id, mode, fps) {
    const feed = { img: img, id: id, mode: mode, fps: fps, timer: null };
    const active = () => feeds.includes(feed) && !paused && !hiddenPause && img.getAttribute("src") !== BLANK;
    img.onerror = () => { if (active()) retry(feed, 2000); };
    img.onload = () => { if (feed.mode === "poll" && active()) retry(feed, 1000); };
    return feed;
  }
  function feedSrc(feed) {
    const url = new URL("camera/" + feed.id + "/" + (feed.mode === "poll" ? "snapshot.jpg" : "stream"), location.href);
    if (feed.mode === "stream" && feed.fps) url.searchParams.set("fps", feed.fps);
    url.searchParams.set("_", Date.now());
    return url.toString();
  }
  function start(feed) { clearTimeout(feed.timer); feed.img.src = feedSrc(feed); }
  function stop(feed) { clearTimeout(feed.timer); feed.img.src = BLANK; }
  function retry(feed, ms) { clearTimeout(feed.timer); feed.timer = setTimeout(() => start(feed), ms); }
  function connect() {
    if (paused || hiddenPause) return;
    feeds.forEach(start);
  }
  function disconnect() { feeds.forEach(stop); }

  // ------------------------------------------------------------------ 表示の切り替え

  function parseHash() {
    const h = location.hash.slice(1);
    if (h === "all") return "all";
    const m = /^camera=(\d+)$/.exec(h);
    return m ? Number(m[1]) : null;
  }
  function valid(sel) { return sel === "all" ? multi() : byId(sel) !== undefined; }
  function initialSelection() {
    let saved = null;
    try { saved = localStorage.getItem("kasumin.camera"); } catch (e) { /* 無視 */ }
    const candidates = [parseHash(), saved === "all" ? "all" : saved !== null && saved !== "" ? Number(saved) : null];
    for (const sel of candidates) if (sel !== null && valid(sel)) return sel;
    return multi() ? "all" : cameras[0].id;
  }

  function show(sel) {
    disconnect();
    feeds = [];
    selected = sel;
    const all = sel === "all";
    viewer.hidden = all;
    grid.hidden = !all;
    btnSnap.hidden = all;
    $("gridHint").hidden = !all;
    if (all) {
      buildGrid();
    } else {
      grid.replaceChildren();
      // 新しい要素にしないと、次の映像が届くまで前のカメラの画像が表示されたままになる
      const img = document.createElement("img");
      img.id = "stream";
      img.alt = "ライブ映像";
      $("stream").replaceWith(img);
      feeds = [makeFeed(img, sel, "stream", fpsCap)];
    }
    buildTabs();
    renderUrls();
    if (multi()) {
      history.replaceState(null, "", "#" + (all ? "all" : "camera=" + sel));
      try { localStorage.setItem("kasumin.camera", String(sel)); } catch (e) { /* 無視 */ }
    }
    if (lastStatus) render(lastStatus);
    connect();
  }

  function buildTabs() {
    camBar.hidden = !multi();
    if (!multi()) { camBar.replaceChildren(); return; }
    const tab = (sel, text, withDot) => {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(sel === selected));
      b.dataset.sel = String(sel);
      if (withDot) {
        const d = document.createElement("span");
        d.className = "dot";
        b.append(d);
      }
      b.append(text);
      b.addEventListener("click", () => { if (sel !== selected) show(sel); });
      return b;
    };
    camBar.replaceChildren(tab("all", "すべて", false), ...cameras.map((c) => tab(c.id, c.name, true)));
  }

  function buildGrid() {
    const poll = !multiplexed && cameras.length > MAX_GRID_STREAMS;
    const fps = Math.min(Number(fpsCap) || GRID_MAX_FPS, GRID_MAX_FPS);
    grid.replaceChildren(...cameras.map((c) => {
      const tile = document.createElement("div");
      tile.className = "tile";
      tile.dataset.id = String(c.id);
      tile.tabIndex = 0;
      tile.setAttribute("role", "button");
      tile.title = c.name + " を大きく表示";
      const img = document.createElement("img");
      img.alt = c.name;
      const label = document.createElement("div");
      label.className = "label";
      const d = document.createElement("span");
      d.className = "dot";
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = c.name;
      const info = document.createElement("span");
      info.className = "info";
      label.append(d, name, info);
      const ov = document.createElement("div");
      ov.className = "overlay";
      ov.append(document.createElement("strong"), document.createElement("span"));
      tile.append(img, label, ov);
      tile.addEventListener("click", () => show(c.id));
      tile.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); show(c.id); }
      });
      feeds.push(makeFeed(img, c.id, poll ? "poll" : "stream", fps));
      return tile;
    }));
  }

  function renderUrls() {
    const base = new URL(".", location.href).toString().replace(/\/$/, "");
    let rows;
    if (!multi()) {
      rows = [["MJPEG ストリーム", base + "/stream"], ["スナップショット", base + "/snapshot.jpg"],
        ["ステータス (JSON)", base + "/api/status"]];
    } else if (selected === "all") {
      rows = cameras.map((c) => [c.name, base + "/camera/" + c.id + "/stream"]);
      rows.push(["ステータス (JSON)", base + "/api/status"]);
    } else {
      const p = base + "/camera/" + selected;
      rows = [["MJPEG ストリーム", p + "/stream"], ["スナップショット", p + "/snapshot.jpg"],
        ["ステータス (JSON)", p + "/api/status"]];
    }
    $("urls").replaceChildren(...rows.map(([text, url]) => {
      const row = document.createElement("div");
      row.className = "row";
      const label = document.createElement("span");
      label.className = "label";
      label.textContent = text;
      const code = document.createElement("code");
      code.textContent = url;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "コピー";
      btn.addEventListener("click", () => copy(btn, code));
      row.append(label, code, btn);
      return row;
    }));
  }

  // ------------------------------------------------------------------ 状態の表示

  function showOverlay(title, text) {
    $("overlayTitle").textContent = title;
    $("overlayText").textContent = text;
    overlay.classList.add("show");
  }
  function hideOverlay() { overlay.classList.remove("show"); }
  function pad(n) { return String(n).padStart(2, "0"); }
  function fmtUptime(sec) {
    sec = Math.floor(sec || 0);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return (d ? d + "日 " : "") + pad(h) + ":" + pad(m) + ":" + pad(sec % 60);
  }
  function noSignalText(c) { return c.error || (c.idle ? "カメラは待機中です" : "再接続しています…"); }

  function render(s) {
    const fido2 = s.auth_mode === "fido2";
    $("authNav").hidden = !fido2;
    $("tokenHint").hidden = !fido2;
    camBar.querySelectorAll("button[data-sel]").forEach((b) => {
      const c = byId(Number(b.dataset.sel));
      const d = b.querySelector(".dot");
      if (c && d) d.classList.toggle("on", !!c.opened);
    });

    if (selected === "all") {
      const live = cameras.filter((c) => c.opened).length;
      dot.classList.toggle("on", live === cameras.length);
      stats.textContent = "カメラ " + cameras.length + " 台 (映像あり " + live + " 台) · 稼働 " + fmtUptime(s.uptime);
      grid.querySelectorAll(".tile").forEach((tile) => {
        const c = byId(Number(tile.dataset.id));
        if (!c) return;
        tile.querySelector(".label .dot").classList.toggle("on", !!c.opened);
        tile.querySelector(".info").textContent = c.opened ? (c.fps || 0).toFixed(1) + " fps" : "";
        const ov = tile.querySelector(".overlay");
        const off = paused || !c.opened;
        ov.classList.toggle("show", off);
        if (off) {
          ov.querySelector("strong").textContent = paused ? "一時停止中" : "カメラ信号なし";
          ov.querySelector("span").textContent = paused ? "" : noSignalText(c);
        }
      });
      return;
    }

    const c = byId(selected) || s;
    dot.classList.toggle("on", !!c.opened);
    const parts = [];
    if (multi()) parts.push(c.name);
    if (c.width && c.height) parts.push(c.width + "×" + c.height);
    parts.push((c.fps || 0).toFixed(1) + " fps");
    parts.push("視聴者 " + c.clients);
    parts.push("稼働 " + fmtUptime(c.uptime));
    stats.textContent = parts.join(" · ");
    if (c.width && c.height) viewer.style.aspectRatio = c.width + " / " + c.height;
    if (paused) {
      showOverlay("一時停止中", "「再開」を押すと配信を再開します");
    } else if (!c.opened) {
      showOverlay("カメラ信号なし", noSignalText(c));
    } else {
      hideOverlay();
    }
  }

  async function poll() {
    try {
      const res = await fetch("api/status", { cache: "no-store" });
      // FIDO2 認証でセッションが切れた (期限切れ・他の端末からログアウトされた) ときはログイン画面へ
      if (res.status === 401 && (res.headers.get("Content-Type") || "").includes("json")) {
        location.replace("login");
        return;
      }
      if (!res.ok) throw new Error("HTTP " + res.status);
      const status = await res.json();
      const list = status.cameras || [status];
      const changed = list.map((c) => c.id).join() !== cameras.map((c) => c.id).join()
        || list.some((c, i) => c.name !== cameras[i].name);
      cameras = list;
      lastStatus = status;
      if (changed || selected === null) {
        show(valid(selected) ? selected : initialSelection()); // サーバーのカメラ構成が変わったら作り直す
      } else if (serverOnline === false) {
        connect(); // サーバー復帰時はストリームを張り直す
      }
      serverOnline = true;
      render(status);
    } catch (e) {
      serverOnline = false;
      dot.classList.remove("on");
      stats.textContent = "サーバーに接続できません";
      showOverlay("サーバーに接続できません", "再接続しています…");
    }
  }

  // ------------------------------------------------------------------ 操作

  async function copy(btn, target) {
    try {
      await navigator.clipboard.writeText(target.textContent);
      btn.textContent = "コピーしました";
    } catch (e) {
      // http:// での LAN アクセスではクリップボード API が使えないため、選択状態にして手動コピーしてもらう
      const range = document.createRange();
      range.selectNodeContents(target);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      btn.textContent = "選択しました";
    }
    setTimeout(() => { btn.textContent = "コピー"; }, 1500);
  }

  btnPause.addEventListener("click", () => {
    paused = !paused;
    btnPause.textContent = paused ? "▶ 再開" : "⏸ 一時停止";
    if (paused) disconnect();
    else connect();
    if (lastStatus) render(lastStatus);
  });
  $("btnReconnect").addEventListener("click", () => {
    paused = false;
    btnPause.textContent = "⏸ 一時停止";
    hideOverlay();
    connect();
    if (lastStatus) render(lastStatus);
  });
  btnSnap.addEventListener("click", () => {
    if (selected === "all" || selected === null) return;
    const t = new Date();
    const a = document.createElement("a");
    a.href = new URL("camera/" + selected + "/snapshot.jpg?_=" + Date.now(), location.href).toString();
    a.download = "kasumin_" + (multi() ? "cam" + selected + "_" : "") + t.getFullYear() + pad(t.getMonth() + 1)
      + pad(t.getDate()) + "_" + pad(t.getHours()) + pad(t.getMinutes()) + pad(t.getSeconds()) + ".jpg";
    document.body.appendChild(a);
    a.click();
    a.remove();
  });
  $("btnFull").addEventListener("click", () => {
    const target = selected === "all" ? grid : viewer;
    if (document.fullscreenElement) document.exitFullscreen();
    else if (target.requestFullscreen) target.requestFullscreen();
  });
  $("btnLogout").addEventListener("click", async () => {
    disconnect();
    try {
      await fetch("api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    } catch (e) { /* 失敗してもログイン画面へ移る */ }
    location.replace("login");
  });
  fpsSel.addEventListener("change", () => {
    fpsCap = fpsSel.value;
    try { localStorage.setItem("kasumin.fps", fpsCap); } catch (e) { /* 無視 */ }
    if (selected !== null) show(selected);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (!paused) { hiddenPause = true; disconnect(); }
    } else if (hiddenPause) {
      hiddenPause = false;
      connect();
    }
  });
  window.addEventListener("hashchange", () => {
    const sel = parseHash();
    if (sel !== null && sel !== selected && valid(sel)) show(sel);
  });

  poll();
  setInterval(poll, POLL_MS);
})();
