// ログイン・設定ページの共通処理 (API の呼び出しと WebAuthn の値の変換)
(function () {
  "use strict";

  function b64urlToBuf(value) {
    const b64 = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }
  function bufToB64url(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function decodeDescriptors(list) {
    return (list || []).map((c) => Object.assign({}, c, { id: b64urlToBuf(c.id) }));
  }

  // サーバーが返す JSON 形式の options を、navigator.credentials に渡せる形にする
  function creationOptions(o) {
    return Object.assign({}, o, {
      challenge: b64urlToBuf(o.challenge),
      user: Object.assign({}, o.user, { id: b64urlToBuf(o.user.id) }),
      excludeCredentials: decodeDescriptors(o.excludeCredentials),
    });
  }
  function requestOptions(o) {
    return Object.assign({}, o, {
      challenge: b64urlToBuf(o.challenge),
      allowCredentials: decodeDescriptors(o.allowCredentials),
    });
  }
  // navigator.credentials の結果を、サーバーへ送る JSON 形式にする
  function credentialToJSON(cred) {
    const r = cred.response;
    const out = {
      id: cred.id,
      rawId: bufToB64url(cred.rawId),
      type: cred.type,
      authenticatorAttachment: cred.authenticatorAttachment || null,
      clientExtensionResults: {},
      response: { clientDataJSON: bufToB64url(r.clientDataJSON) },
    };
    if (r.attestationObject) {
      out.response.attestationObject = bufToB64url(r.attestationObject);
      out.response.transports = typeof r.getTransports === "function" ? r.getTransports() : [];
    } else {
      out.response.authenticatorData = bufToB64url(r.authenticatorData);
      out.response.signature = bufToB64url(r.signature);
      if (r.userHandle) out.response.userHandle = bufToB64url(r.userHandle);
    }
    return out;
  }

  // body を渡すと POST、省略すると GET。失敗したときはサーバーのメッセージ付きで例外を投げる
  async function api(path, body) {
    const init = { cache: "no-store", credentials: "same-origin" };
    if (body !== undefined) {
      init.method = "POST";
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    const res = await fetch(path, init);
    let data = null;
    try { data = await res.json(); } catch (e) { /* JSON 以外の応答 */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || "サーバーエラー (HTTP " + res.status + ")");
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // 登録・ログインの開始 (チャレンジの取得) を、ボタンが押される前に済ませておく。
  // Safari などはボタン操作の直後でないとキーを使わせないため、押してから通信を待つと失敗することがある
  function prepared(begin) {
    let entry = null;
    function refresh() {
      entry = { at: Date.now(), promise: begin() };
      entry.promise.catch(() => { /* エラーは使うときに表示する */ });
      return entry.promise;
    }
    return {
      refresh: refresh,
      take: function () {
        // サーバー側の有効期限 (5 分) が切れる前に取り直す
        if (!entry || Date.now() - entry.at > 4 * 60 * 1000) refresh();
        const promise = entry.promise;
        entry = null;
        return promise;
      },
    };
  }

  async function createCredential(begin) {
    const cred = await navigator.credentials.create({ publicKey: creationOptions(begin.publicKey) });
    return credentialToJSON(cred);
  }
  async function getCredential(begin) {
    const cred = await navigator.credentials.get({ publicKey: requestOptions(begin.publicKey) });
    return credentialToJSON(cred);
  }

  function describeError(e) {
    switch (e && e.name) {
      case "NotAllowedError":
        return "キーの操作がキャンセルされたか、時間切れになりました。もう一度お試しください。";
      case "InvalidStateError":
        return "このキーは登録済みです。";
      case "SecurityError":
        return "このアドレスではパスキーを使えません。公開 URL から開いてください。";
      case "NotSupportedError":
        return "このキーまたはブラウザは対応していません。";
      default:
        return (e && e.message) || String(e);
    }
  }

  function guessDeviceName() {
    const ua = navigator.userAgent;
    if (/iPhone/.test(ua)) return "iPhone";
    if (/iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return "iPad";
    if (/Android/.test(ua)) return "Android";
    if (/Windows/.test(ua)) return "Windows";
    if (/Macintosh/.test(ua)) return "Mac";
    return "";
  }
  function describeAgent(ua) {
    if (!ua) return "不明なブラウザ";
    const browser = /Edg(e|A|iOS)?\//.test(ua) ? "Edge"
      : /OPR\//.test(ua) ? "Opera"
      : /(Firefox|FxiOS)\//.test(ua) ? "Firefox"
      : /(Chrome|CriOS)\//.test(ua) ? "Chrome"
      : /Safari\//.test(ua) ? "Safari" : "ブラウザ";
    const os = /iPhone/.test(ua) ? "iPhone"
      : /iPad/.test(ua) ? "iPad"
      : /Android/.test(ua) ? "Android"
      : /Windows/.test(ua) ? "Windows"
      : /Mac OS X/.test(ua) ? "Mac"
      : /Linux/.test(ua) ? "Linux" : "";
    return os ? browser + " / " + os : browser;
  }
  function formatTime(sec) {
    if (!sec) return "-";
    return new Date(sec * 1000).toLocaleString("ja-JP", { dateStyle: "short", timeStyle: "short" });
  }

  // コード片をクリップボードへコピーする。使えない環境では選択状態にして手動コピーしてもらう
  async function copyText(btn, target) {
    const label = btn.textContent;
    try {
      await navigator.clipboard.writeText(target.textContent);
      btn.textContent = "コピーしました";
    } catch (e) {
      const range = document.createRange();
      range.selectNodeContents(target);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      btn.textContent = "選択しました";
    }
    setTimeout(() => { btn.textContent = label; }, 1500);
  }

  window.KasuminAuth = {
    supported: !!(window.PublicKeyCredential && navigator.credentials && navigator.credentials.create),
    api: api,
    prepared: prepared,
    createCredential: createCredential,
    getCredential: getCredential,
    describeError: describeError,
    guessDeviceName: guessDeviceName,
    describeAgent: describeAgent,
    formatTime: formatTime,
    copyText: copyText,
  };
})();
