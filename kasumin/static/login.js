// ログイン (/login) と、登録リンクからのキー登録 (/setup#t=...) のページ
(function () {
  "use strict";
  const A = window.KasuminAuth;
  const $ = (id) => document.getElementById(id);
  const btn = $("btnGo");
  const msg = $("msg");
  const setupMode = location.pathname.replace(/\/+$/, "") === "/setup";
  // 登録リンクのトークンは # 以降にあるので、サーバーのアクセスログには残らない
  const setupToken = new URLSearchParams(location.hash.slice(1)).get("t") || "";
  const nextParam = new URLSearchParams(location.search).get("next");
  const next = ["/", "/settings"].includes(nextParam) ? nextParam : "/";

  const begin = A.prepared(() => setupMode
    ? A.api("/api/auth/register/begin", { setup_token: setupToken })
    : A.api("/api/auth/login/begin", {}));

  function show(text, kind) {
    msg.textContent = text;
    msg.className = "msg" + (kind ? " " + kind : "");
  }

  async function run() {
    btn.disabled = true;
    show(setupMode ? "キーを登録しています…" : "キーを確認しています…");
    try {
      const options = await begin.take();
      if (setupMode) {
        const credential = await A.createCredential(options);
        await A.api("/api/auth/register/complete", {
          ceremony: options.ceremony, credential: credential, name: $("keyName").value,
        });
        show("登録しました。カメラの画面に移動します…", "ok");
        location.replace("/");
      } else {
        const credential = await A.getCredential(options);
        await A.api("/api/auth/login/complete", { ceremony: options.ceremony, credential: credential });
        show("ログインしました", "ok");
        location.replace(next);
      }
    } catch (e) {
      show(A.describeError(e), "error");
      btn.disabled = false;
      begin.refresh();
    }
  }

  // 公開 URL 以外 (LAN の IP アドレスなど) で開かれている場合は、正しい URL へ案内する
  function showWrongOrigin(origin) {
    const url = origin + location.pathname + location.search + location.hash;
    const link = document.createElement("a");
    link.href = url;
    link.textContent = url;
    msg.className = "msg error";
    msg.replaceChildren("パスキーは公開 URL でしか使えません。次の URL から開いてください: ", link);
  }

  async function init() {
    if (setupMode) {
      document.title = "Kasumin キーの登録";
      $("title").textContent = "キーの登録";
      $("lead").textContent = "このカメラにログインするためのパスキー (スマートフォンや PC の生体認証・PIN) "
        + "またはセキュリティキーを登録します。";
      $("nameField").hidden = false;
      $("keyName").value = A.guessDeviceName();
      btn.textContent = "パスキー / セキュリティキーを登録";
    }

    let state;
    try {
      state = await A.api("/api/auth/state");
    } catch (e) {
      show("サーバーに接続できません: " + e.message, "error");
      return;
    }
    if (location.origin !== state.origin) {
      showWrongOrigin(state.origin);
      return;
    }
    if (!A.supported) {
      show("このブラウザはパスキー (WebAuthn) に対応していません。最新のブラウザで開いてください。", "error");
      return;
    }
    if (setupMode) {
      if (!setupToken) {
        show("登録リンクが正しくありません。サーバーの起動画面または設定画面に表示された URL を、そのまま開いてください。", "error");
        return;
      }
    } else if (state.authenticated) {
      location.replace(next);
      return;
    } else if (!state.has_keys) {
      show("キーがまだ登録されていません。サーバーの起動画面に表示される登録リンクを開いてください。", "error");
      return;
    }

    begin.refresh().catch((e) => {
      // 期限切れ・使用済みの登録リンクは、押す前に知らせる
      show(A.describeError(e), "error");
      if (e.status === 403) btn.disabled = true;
    });
    btn.disabled = false;
    btn.addEventListener("click", run);
    $("keyName").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !btn.disabled) run();
    });
  }

  init();
})();
