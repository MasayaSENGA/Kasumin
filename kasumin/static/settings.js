// 設定ページ: キー・アクセストークン・セッションの管理
(function () {
  "use strict";
  const A = window.KasuminAuth;
  const $ = (id) => document.getElementById(id);
  const addKey = A.prepared(() => A.api("/api/auth/register/begin", {}));
  let data = null;

  function show(id, text, kind) {
    const el = $(id);
    el.textContent = text || "";
    el.className = "msg" + (kind ? " " + kind : "");
  }
  function fail(id, e) {
    if (e && e.status === 401) {
      location.replace("/login?next=/settings");
      return;
    }
    show(id, A.describeError(e), "error");
  }

  // 一覧の 1 行 (名前・バッジ・補足情報・ボタン)。利用者が付けた名前は textContent でだけ表示する
  function item(title, badges, meta, action) {
    const name = document.createElement("strong");
    name.textContent = title;
    const detail = document.createElement("div");
    detail.className = "meta";
    detail.textContent = meta;
    const info = document.createElement("div");
    info.className = "info";
    info.append(name, ...badges, detail);
    const li = document.createElement("li");
    li.append(info);
    if (action) li.append(action);
    return li;
  }
  function badge(text, warn) {
    const el = document.createElement("span");
    el.className = "badge" + (warn ? " warn" : "");
    el.textContent = text;
    return el;
  }
  function button(text, onClick) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "danger";
    el.textContent = text;
    el.addEventListener("click", onClick);
    return el;
  }
  function empty(text) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = text;
    return li;
  }

  function keyKind(k) {
    if (k.backup_eligible) return "同期されるパスキー";
    if (k.attachment === "platform") return "この端末のパスキー";
    if (k.attachment === "cross-platform") return "セキュリティキー / 別の端末";
    return "キー";
  }

  function renderKeys() {
    const active = data.keys.filter((k) => k.rp_id === data.rp_id).length;
    $("keys").replaceChildren(...data.keys.map((k) => {
      const badges = [];
      if (k.id === data.current_key) badges.push(badge("このセッションで使用中"));
      if (k.rp_id !== data.rp_id) badges.push(badge(k.rp_id + " 用 (この URL では使えません)", true));
      const meta = keyKind(k) + " · 登録 " + A.formatTime(k.created) + " · 最終使用 " + A.formatTime(k.last_used);
      const del = button("削除", () => deleteKey(k));
      if (k.rp_id === data.rp_id && active <= 1) {
        del.disabled = true;
        del.title = "最後のキーは削除できません。先に別のキーを追加してください";
      }
      return item(k.name, badges, meta, del);
    }));
  }

  function renderTokens() {
    if (!data.tokens.length) {
      $("tokens").replaceChildren(empty("発行済みのアクセストークンはありません"));
      return;
    }
    $("tokens").replaceChildren(...data.tokens.map((t) => item(
      t.name, [], "発行 " + A.formatTime(t.created) + " · 最終使用 " + A.formatTime(t.last_used),
      button("削除", () => deleteToken(t)),
    )));
  }

  function renderSessions() {
    $("sessions").replaceChildren(...data.sessions.map((s) => {
      const current = s.id === data.current_session;
      const meta = (s.ip || "接続元不明") + " · キー「" + s.key + "」 · ログイン " + A.formatTime(s.created)
        + " · 最終アクセス " + A.formatTime(s.last_seen);
      return item(
        A.describeAgent(s.user_agent), current ? [badge("このセッション")] : [], meta,
        current ? null : button("ログアウト", () => revokeSession(s)),
      );
    }));
    $("btnRevokeOthers").disabled = data.sessions.length <= 1;
  }

  async function load() {
    try {
      data = await A.api("/api/auth/overview");
    } catch (e) {
      fail("keyMsg", e);
      return;
    }
    renderKeys();
    renderTokens();
    renderSessions();
  }

  async function deleteKey(k) {
    if (!confirm("キー「" + k.name + "」を削除しますか？\nこのキーでログインしているセッションも切断されます。")) return;
    try {
      await A.api("/api/auth/keys/delete", { id: k.id });
    } catch (e) {
      fail("keyMsg", e);
      return;
    }
    if (k.id === data.current_key) {
      location.replace("/login");
      return;
    }
    show("keyMsg", "キー「" + k.name + "」を削除しました", "ok");
    load();
  }

  async function addKeyHere() {
    const btn = $("btnAddKey");
    btn.disabled = true;
    show("keyMsg", "キーを登録しています…");
    try {
      const options = await addKey.take();
      const credential = await A.createCredential(options);
      const res = await A.api("/api/auth/register/complete", {
        ceremony: options.ceremony, credential: credential, name: $("newKeyName").value || A.guessDeviceName(),
      });
      $("newKeyName").value = "";
      show("keyMsg", "キー「" + res.key.name + "」を登録しました", "ok");
      load();
    } catch (e) {
      fail("keyMsg", e);
    } finally {
      btn.disabled = false;
      addKey.refresh();
    }
  }

  async function invite() {
    try {
      const res = await A.api("/api/auth/invite", {});
      $("inviteUrl").textContent = res.url;
      $("inviteExpires").textContent = A.formatTime(res.expires);
      $("inviteBox").hidden = false;
      show("keyMsg", "");
    } catch (e) {
      fail("keyMsg", e);
    }
  }

  async function createToken() {
    const btn = $("btnCreateToken");
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const res = await A.api("/api/auth/tokens/create", { name: $("newTokenName").value });
      $("newTokenName").value = "";
      $("tokenValue").textContent = res.token;
      $("tokenStream").textContent = location.origin + "/stream?token=" + res.token;
      $("tokenSnap").textContent = location.origin + "/snapshot.jpg?token=" + res.token;
      $("tokenBox").hidden = false;
      show("tokenMsg", "アクセストークン「" + res.name + "」を発行しました", "ok");
      load();
    } catch (e) {
      fail("tokenMsg", e);
    } finally {
      btn.disabled = false;
    }
  }

  async function deleteToken(t) {
    if (!confirm("アクセストークン「" + t.name + "」を削除しますか？\nこのトークンを設定したアプリは映像を取得できなくなります。")) return;
    try {
      await A.api("/api/auth/tokens/delete", { id: t.id });
    } catch (e) {
      fail("tokenMsg", e);
      return;
    }
    show("tokenMsg", "アクセストークン「" + t.name + "」を削除しました", "ok");
    load();
  }

  async function revokeSession(s) {
    try {
      await A.api("/api/auth/sessions/delete", { id: s.id });
    } catch (e) {
      fail("sessionMsg", e);
      return;
    }
    show("sessionMsg", "セッションをログアウトさせました", "ok");
    load();
  }

  async function revokeOthers() {
    if (!confirm("このセッション以外のすべてのセッションをログアウトさせますか？")) return;
    try {
      const res = await A.api("/api/auth/sessions/delete-others", {});
      show("sessionMsg", res.count + " 件のセッションをログアウトさせました", "ok");
    } catch (e) {
      fail("sessionMsg", e);
      return;
    }
    load();
  }

  $("btnLogout").addEventListener("click", async () => {
    try { await A.api("/api/auth/logout", {}); } catch (e) { /* 失敗してもログイン画面へ移る */ }
    location.replace("/login");
  });
  $("btnAddKey").addEventListener("click", addKeyHere);
  $("newKeyName").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !$("btnAddKey").disabled) addKeyHere();
  });
  $("btnInvite").addEventListener("click", invite);
  $("btnCreateToken").addEventListener("click", createToken);
  $("newTokenName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") createToken();
  });
  $("btnRevokeOthers").addEventListener("click", revokeOthers);
  document.querySelectorAll("button[data-copy]").forEach((btn) => {
    btn.addEventListener("click", () => A.copyText(btn, $(btn.dataset.copy)));
  });

  if (A.supported) {
    addKey.refresh();
  } else {
    $("btnAddKey").disabled = true;
    $("btnAddKey").title = "このブラウザはパスキーに対応していません";
  }
  load();
})();
