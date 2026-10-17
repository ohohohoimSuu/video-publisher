(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.LoginBridge = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const BASE_URL = "http://127.0.0.1:8765";
  const PLATFORMS = Object.freeze({
    douyin: Object.freeze({ id: "douyin", name: "抖音", domain: "douyin.com", pageHosts: Object.freeze(["creator.douyin.com", "www.douyin.com"]), origins: Object.freeze(["https://*.douyin.com/*"]) }),
    xiaohongshu: Object.freeze({ id: "xiaohongshu", name: "小红书", domain: "xiaohongshu.com", pageHosts: Object.freeze(["creator.xiaohongshu.com", "www.xiaohongshu.com"]), origins: Object.freeze(["https://*.xiaohongshu.com/*"]) }),
    youtube: Object.freeze({ id: "youtube", name: "YouTube", domain: "youtube.com", pageHosts: Object.freeze(["studio.youtube.com"]), origins: Object.freeze(["https://*.youtube.com/*"]) })
  });
  const MESSAGES = Object.freeze({
    unsupported: "请先打开抖音或小红书的创作者/官网页面，或 YouTube Studio 频道页，再打开扩展。哔哩哔哩请使用发布台的独立授权。",
    youtube_channel: "请打开已登录的 YouTube Studio 频道页（studio.youtube.com/channel/UC…），再打开扩展。",
    youtube_context: "当前 YouTube Studio 账号选择参数无效或重复，请重新打开目标 Studio 频道页面后同步。",
    incognito: "不支持无痕窗口，请在常用 Chrome 的普通窗口操作。",
    invalid_code: "请输入发布台生成的 64 位连接码。",
    permission_denied: "未获得当前平台访问权限，尚未读取或同步登录。",
    permission_failed: "Chrome 未授予当前平台访问权限，请重新打开扩展后点击同步。",
    unavailable: "无法连接本地发布台，请先启动发布台后重试。",
    invalid_handshake: "连接码无效、已过期，或本地发布台响应异常，请重新生成连接码。",
    platform_mismatch: "连接码绑定的平台与当前页面不同，请切换到对应平台后重试。",
    tab_changed: "当前页面已变化，请回到目标平台页面，重新打开扩展。",
    store_unavailable: "无法确认当前标签页的 CookieStore，未读取登录数据。",
    cookie_scope: "会话数据未通过平台范围检查，已取消同步。",
    partitioned_session: "当前会话含分区 Cookie，本版本暂不支持完整同步该分区会话。请使用发布台的独立授权。",
    storage_unavailable: "无法读取当前页面登录存储，请等待页面加载后重试。",
    too_large: "当前平台存储超过安全大小限制，已取消同步。",
    empty_session: "当前平台没有可同步的 Cookie，请确认已在此 Chrome 登录。",
    import_failed: "本地发布台未确认接收。请查看发布台状态，必要时重新生成连接码。",
    busy: "正在同步，请等待完成。",
    unexpected: "同步未完成，请重新打开扩展后重试。"
  });

  class BridgeError extends Error {
    constructor(code) { super(MESSAGES[code] || MESSAGES.unexpected); this.code = code; }
  }

  function domainMatches(host, domain) {
    return typeof host === "string" && /^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?)*$/.test(host)
      && (host === domain || host.endsWith("." + domain));
  }

  function platformFromUrl(raw) {
    try {
      const url = new URL(raw);
      if (url.protocol !== "https:" || url.port || url.username || url.password) return null;
      return Object.values(PLATFORMS).find((platform) => domainMatches(url.hostname, platform.domain)) || null;
    } catch (_) { return null; }
  }

  function youtubeAuthuser(page) {
    const values = page.searchParams.getAll("authuser");
    if (values.length === 0) return null;
    if (values.length !== 1 || !/^(?:0|[1-9][0-9]?)$/.test(values[0])) throw new BridgeError("youtube_context");
    return Number(values[0]);
  }

  function validateTab(tab, expected) {
    if (!tab || tab.incognito) throw new BridgeError(tab && tab.incognito ? "incognito" : "unsupported");
    const platform = platformFromUrl(tab.url);
    if (!platform || !Number.isInteger(tab.id)) throw new BridgeError("unsupported");
    const page = new URL(tab.url);
    if (platform.id === "youtube" && (page.hostname !== "studio.youtube.com" || !/^\/channel\/UC[A-Za-z0-9_-]{22}(?:\/|$)/.test(page.pathname))) {
      throw new BridgeError("youtube_channel");
    }
    if (platform.id === "youtube") youtubeAuthuser(page);
    if (!platform.pageHosts.includes(page.hostname)) throw new BridgeError("unsupported");
    if (expected && (tab.id !== expected.id || new URL(tab.url).origin !== new URL(expected.url).origin || tab.active === false)) {
      throw new BridgeError("tab_changed");
    }
    if (expected && platform.id === "youtube" && page.pathname.split("/")[2] !== new URL(expected.url).pathname.split("/")[2]) {
      throw new BridgeError("tab_changed");
    }
    if (expected && platform.id === "youtube" && youtubeAuthuser(page) !== youtubeAuthuser(new URL(expected.url))) {
      throw new BridgeError("tab_changed");
    }
    return platform;
  }

  function normalizeCode(code) {
    const value = typeof code === "string" ? code.trim() : "";
    if (!/^[a-f0-9]{64}$/i.test(value)) throw new BridgeError("invalid_code");
    return value.toLowerCase();
  }

  function expiryTime(value) {
    // The local service normally returns an ISO timestamp; UNIX seconds are also accepted.
    if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
    return typeof value === "string" ? Date.parse(value) : NaN;
  }

  function normalizeCookies(cookies, platform, storeId) {
    if (!Array.isArray(cookies) || cookies.length > 512) throw new BridgeError("too_large");
    let total = 0;
    const selected = [];
    for (const cookie of cookies) {
      if (!cookie || typeof cookie.domain !== "string" || cookie.storeId !== storeId) throw new BridgeError("cookie_scope");
      const host = cookie.domain.startsWith(".") ? cookie.domain.slice(1) : cookie.domain;
      if (!domainMatches(host, platform.domain)) throw new BridgeError("cookie_scope");
      // Refuse incomplete imports when Chrome exposes a partitioned session cookie.
      if (cookie.partitionKey != null || cookie.partitioned === true) throw new BridgeError("partitioned_session");
      if (typeof cookie.name !== "string" || typeof cookie.value !== "string" || typeof cookie.path !== "string" || !cookie.path.startsWith("/")) {
        throw new BridgeError("cookie_scope");
      }
      total += cookie.name.length + cookie.value.length + cookie.domain.length + cookie.path.length;
      if (total > 1024 * 1024) throw new BridgeError("too_large");
      const item = {
        name: cookie.name, value: cookie.value, domain: cookie.domain, path: cookie.path,
        secure: Boolean(cookie.secure), httpOnly: Boolean(cookie.httpOnly),
        sameSite: ["no_restriction", "lax", "strict", "unspecified"].includes(cookie.sameSite) ? cookie.sameSite : "unspecified",
        hostOnly: Boolean(cookie.hostOnly), session: Boolean(cookie.session)
      };
      if (Number.isFinite(cookie.expirationDate)) item.expirationDate = cookie.expirationDate;
      selected.push(item);
    }
    if (!selected.length) throw new BridgeError("empty_session");
    return selected;
  }

  // Serialized by chrome.scripting. This function has no access to extension variables.
  function readCurrentOriginStorage(expectedOrigin) {
    if (location.protocol !== "https:" || location.origin !== expectedOrigin || window.top !== window) return { failure: "tab_changed" };
    try {
      if (localStorage.length > 256) return { failure: "too_large" };
      const entries = [];
      let size = 0;
      const encoder = new TextEncoder();
      for (let index = 0; index < localStorage.length; index++) {
        const name = localStorage.key(index);
        if (typeof name !== "string") continue;
        const value = localStorage.getItem(name);
        if (typeof value !== "string") continue;
        const nameBytes = encoder.encode(name).byteLength;
        const valueBytes = encoder.encode(value).byteLength;
        if (nameBytes > 2048 || valueBytes > 128 * 1024) return { failure: "too_large" };
        size += nameBytes + valueBytes;
        if (size > 1024 * 1024) return { failure: "too_large" };
        entries.push({ name, value });
      }
      return { origin: location.origin, localStorage: entries };
    } catch (_) { return { failure: "storage_unavailable" }; }
  }

  function validateOriginResult(results, expectedOrigin) {
    if (!Array.isArray(results) || results.length !== 1 || results[0].frameId !== 0) throw new BridgeError("storage_unavailable");
    const result = results[0].result;
    if (result && ["too_large", "tab_changed", "storage_unavailable"].includes(result.failure)) throw new BridgeError(result.failure);
    if (!result || result.origin !== expectedOrigin || !Array.isArray(result.localStorage) || result.localStorage.length > 256) {
      throw new BridgeError("storage_unavailable");
    }
    let size = 0;
    const encoder = new TextEncoder();
    const localStorage = result.localStorage.map((entry) => {
      if (!entry || typeof entry.name !== "string" || typeof entry.value !== "string") throw new BridgeError("storage_unavailable");
      const nameBytes = encoder.encode(entry.name).byteLength;
      const valueBytes = encoder.encode(entry.value).byteLength;
      if (nameBytes > 2048 || valueBytes > 128 * 1024) throw new BridgeError("too_large");
      size += nameBytes + valueBytes;
      if (size > 1024 * 1024) throw new BridgeError("too_large");
      return { name: entry.name, value: entry.value };
    });
    return { origin: expectedOrigin, localStorage };
  }

  async function postJson(fetcher, path, body, code) {
    const encodedBody = JSON.stringify(body);
    if (new TextEncoder().encode(encodedBody).byteLength > 2 * 1024 * 1024) throw new BridgeError("too_large");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetcher(BASE_URL + path, {
        method: "POST", cache: "no-store", credentials: "omit", redirect: "error",
        headers: { "Content-Type": "application/json", ...(code ? { "X-Bridge-Code": code } : {}) },
        body: encodedBody, signal: controller.signal
      });
      if (!response.ok) throw new BridgeError(path.endsWith("handshake") ? "invalid_handshake" : "import_failed");
      try { return await response.json(); }
      catch (_) { throw new BridgeError(path.endsWith("handshake") ? "invalid_handshake" : "import_failed"); }
    } catch (error) {
      if (error instanceof BridgeError) throw error;
      throw new BridgeError(path.endsWith("handshake") ? "unavailable" : "import_failed");
    } finally { clearTimeout(timer); }
  }

  function createBridge(chromeApi, fetcher, now = Date.now) {
    let running = false;

    // Keep this function non-async: permissions.request must run directly in the click call stack.
    function syncFromClick(tab, rawCode) {
      if (running) return Promise.reject(new BridgeError("busy"));
      let platform, code, permission;
      try {
        platform = validateTab(tab);
        code = normalizeCode(rawCode);
        running = true;
        permission = chromeApi.permissions.request({ origins: [...platform.origins] });
      } catch (error) {
        running = false;
        return Promise.reject(error instanceof BridgeError ? error : new BridgeError("permission_failed"));
      }

      return (async function () {
        let granted = false;
        try {
          try { granted = await permission; }
          catch (_) { throw new BridgeError("permission_failed"); }
          if (!granted) throw new BridgeError("permission_denied");
          const handshake = await postJson(fetcher, "/api/browser/handshake", { code });
          if (!handshake || !Object.hasOwn(PLATFORMS, handshake.platform) || !Number.isFinite(expiryTime(handshake.expires_at)) || expiryTime(handshake.expires_at) <= now()) {
            throw new BridgeError("invalid_handshake");
          }
          if (handshake.platform !== platform.id) throw new BridgeError("platform_mismatch");
          validateTab(await chromeApi.tabs.get(tab.id), tab);
          const stores = await chromeApi.cookies.getAllCookieStores();
          const matches = Array.isArray(stores) ? stores.filter((store) => Array.isArray(store.tabIds) && store.tabIds.includes(tab.id)) : [];
          if (matches.length !== 1 || typeof matches[0].id !== "string" || (tab.storeId && tab.storeId !== matches[0].id)) {
            throw new BridgeError("store_unavailable");
          }
          const origin = new URL(tab.url).origin;
          const origins = [validateOriginResult(await chromeApi.scripting.executeScript({
            target: { tabId: tab.id, frameIds: [0] }, world: "ISOLATED", func: readCurrentOriginStorage, args: [origin]
          }), origin)];
          validateTab(await chromeApi.tabs.get(tab.id), tab);
          const storeId = matches[0].id;
          const cookies = normalizeCookies(await chromeApi.cookies.getAll({ domain: platform.domain, storeId }), platform, storeId);
          validateTab(await chromeApi.tabs.get(tab.id), tab);
          const pageUrl = new URL(tab.url);
          const authuser = platform.id === "youtube" ? youtubeAuthuser(pageUrl) : null;
          const result = await postJson(fetcher, "/api/browser/import", {
            platform: platform.id, cookies, origins,
            page_url: pageUrl.origin + pageUrl.pathname + (authuser === null ? "" : `?authuser=${authuser}`), account_hint: ""
          }, code);
          if (!result || result.accepted !== true || result.platform !== platform.id || !result.job || typeof result.job.id !== "string" || typeof result.job.status !== "string") {
            throw new BridgeError("import_failed");
          }
          // No session data or pairing code is returned to the popup UI.
          return { platform: platform.id, name: platform.name, cookieCount: cookies.length, jobId: result.job.id };
        } catch (error) {
          throw error instanceof BridgeError ? error : new BridgeError("unexpected");
        } finally {
          code = "";
          if (granted) {
            try { await chromeApi.permissions.remove({ origins: [...platform.origins] }); }
            catch (_) { /* The extension has no background reader even if Chrome retains a grant. */ }
          }
          running = false;
        }
      })();
    }

    return Object.freeze({ syncFromClick });
  }

  return Object.freeze({ BASE_URL, PLATFORMS, BridgeError, MESSAGES, domainMatches, platformFromUrl, validateTab, normalizeCode, normalizeCookies, readCurrentOriginStorage, validateOriginResult, createBridge });
});
