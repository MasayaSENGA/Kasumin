(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const img = $("stream");
  const viewer = $("viewer");
  const overlay = $("overlay");
  const dot = $("dot");
  const stats = $("stats");
  const btnPause = $("btnPause");
  const fpsSel = $("fpsSel");
  // 1x1 の透明 GIF。src を差し替えると進行中の MJPEG 受信が中断される
  const BLANK = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
  const POLL_MS = 2000;

  let paused = false;      // ユーザー操作による一時停止
  let hiddenPause = false; // タブが非表示のため自動停止中
  let serverOnline = null; // null = 未確認
  let retryTimer = null;
  let fpsCap = "";
  try { fpsCap = localStorage.getItem("kasumin.fps") || ""; } catch (e) { /* 使えない環境では無視 */ }
  fpsSel.value = fpsCap;
  if (fpsSel.value !== fpsCap) { fpsCap = ""; fpsSel.value = ""; }

  function streamUrl() {
    const url = new URL("stream", location.href);
    if (fpsCap) url.searchParams.set("fps", fpsCap);
    url.searchParams.set("_", Date.now());
    return url.toString();
  }
  function connect() {
    clearTimeout(retryTimer);
    if (paused || hiddenPause) return;
    img.src = streamUrl();
  }
  function disconnect() {
    clearTimeout(retryTimer);
    img.src = BLANK;
  }
  function scheduleRetry(ms) {
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, ms);
  }
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

  function render(s) {
    dot.classList.toggle("on", !!s.opened);
    const parts = [];
    if (s.width && s.height) parts.push(s.width + "×" + s.height);
    parts.push((s.fps || 0).toFixed(1) + " fps");
    parts.push("視聴者 " + s.clients);
    parts.push("稼働 " + fmtUptime(s.uptime));
    stats.textContent = parts.join(" · ");
    const fido2 = s.auth_mode === "fido2";
    $("authNav").hidden = !fido2;
    $("tokenHint").hidden = !fido2;
    if (s.width && s.height) viewer.style.aspectRatio = s.width + " / " + s.height;
    if (paused) {
      showOverlay("一時停止中", "「再開」を押すと配信を再開します");
    } else if (!s.opened) {
      showOverlay("カメラ信号なし", s.error || (s.idle ? "カメラは待機中です" : "再接続しています…"));
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
      if (serverOnline === false) connect(); // サーバー復帰時はストリームを張り直す
      serverOnline = true;
      render(status);
    } catch (e) {
      serverOnline = false;
      dot.classList.remove("on");
      stats.textContent = "サーバーに接続できません";
      showOverlay("サーバーに接続できません", "再接続しています…");
    }
  }

  img.addEventListener("error", () => {
    if (img.getAttribute("src") === BLANK || paused || hiddenPause) return;
    scheduleRetry(2000);
  });

  btnPause.addEventListener("click", () => {
    paused = !paused;
    btnPause.textContent = paused ? "▶ 再開" : "⏸ 一時停止";
    if (paused) {
      disconnect();
      showOverlay("一時停止中", "「再開」を押すと配信を再開します");
    } else {
      hideOverlay();
      connect();
    }
  });
  $("btnReconnect").addEventListener("click", () => {
    paused = false;
    btnPause.textContent = "⏸ 一時停止";
    hideOverlay();
    connect();
  });
  $("btnSnap").addEventListener("click", () => {
    const t = new Date();
    const a = document.createElement("a");
    a.href = new URL("snapshot.jpg?_=" + Date.now(), location.href).toString();
    a.download = "kasumin_" + t.getFullYear() + pad(t.getMonth() + 1) + pad(t.getDate())
      + "_" + pad(t.getHours()) + pad(t.getMinutes()) + pad(t.getSeconds()) + ".jpg";
    document.body.appendChild(a);
    a.click();
    a.remove();
  });
  $("btnFull").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else if (viewer.requestFullscreen) viewer.requestFullscreen();
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
    connect();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (!paused) { hiddenPause = true; disconnect(); }
    } else if (hiddenPause) {
      hiddenPause = false;
      connect();
    }
  });

  // 他のアプリから使うための URL
  const base = new URL(".", location.href).toString().replace(/\/$/, "");
  $("urlStream").textContent = base + "/stream";
  $("urlSnap").textContent = base + "/snapshot.jpg";
  $("urlStatus").textContent = base + "/api/status";
  document.querySelectorAll("button[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const target = $(btn.dataset.copy);
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
    });
  });

  connect();
  poll();
  setInterval(poll, POLL_MS);
})();
