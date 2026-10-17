"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { spawnSync } = require("node:child_process");
const bridge = require("../chrome-extension/bridge.js");
const extensionDir = path.resolve(__dirname, "../chrome-extension");
const CODE = "a".repeat(64);
const NOW = Date.parse("2026-10-04T12:00:00Z");
const CHANNEL = "UC" + "A".repeat(22);

function fixture(options = {}) {
  const tab = { id: 17, url: "https://creator.douyin.com/creator-micro/home?private=synthetic#fragment", active: true, incognito: false, ...options.tab };
  const platform = bridge.platformFromUrl(tab.url) || bridge.PLATFORMS.douyin;
  const calls = [];
  const storeId = "profile-store-2";
  const cookie = { domain: "." + platform.domain, name: { douyin: "sessionid", xiaohongshu: "web_session", youtube: "SID" }[platform.id], value: "synthetic-cookie-not-real", path: "/", storeId, secure: true, httpOnly: true, sameSite: "lax", session: false, hostOnly: false, expirationDate: Math.floor(Date.now() / 1000) + 3600 };
  let tabReads = 0;
  const chrome = {
    permissions: {
      request: (details) => { calls.push({ type: "request", details }); return Promise.resolve(options.permission !== false); },
      remove: async (details) => { calls.push({ type: "remove", details }); return true; }
    },
    tabs: {
      get: async (id) => {
        calls.push({ type: "tab", id });
        tabReads++;
        return options.getTab ? options.getTab(tabReads, tab) : tab;
      },
      query: async (details) => { calls.push({ type: "query", details }); return [tab]; }
    },
    cookies: {
      getAllCookieStores: async () => {
        calls.push({ type: "stores" });
        return options.stores || [{ id: "another-profile", tabIds: [1, 2] }, { id: storeId, tabIds: [17, 23] }];
      },
      getAll: async (details) => { calls.push({ type: "cookies", details }); return options.cookies || [cookie]; }
    },
    scripting: {
      executeScript: async (details) => {
        calls.push({ type: "script", details });
        return options.scriptResults || [{ frameId: 0, result: { origin: new URL(tab.url).origin, localStorage: [{ name: "synthetic_state", value: "synthetic-local-storage-not-real" }] } }];
      }
    },
    storage: new Proxy({}, { get() { throw new Error("Persistent storage must never be used"); } })
  };
  const fetcher = async (url, init) => {
    const body = JSON.parse(init.body);
    const type = url.endsWith("handshake") ? "handshake" : "import";
    calls.push({ type, url, init, body });
    if (options.fetchError) throw new Error(options.fetchError);
    if (options.invalidJson) return { ok: true, json: async () => { throw new Error("synthetic secret error"); } };
    const result = type === "handshake"
      ? { platform: platform.id, name: "测试账号", expires_at: "2026-10-04T12:05:00Z", ...options.handshake }
      : { accepted: true, platform: platform.id, job: { id: "test-job", status: "queued" }, ...options.importResult };
    return { ok: options.httpError !== type, json: async () => result };
  };
  return { tab, cookie, storeId, chrome, fetcher, calls, client: bridge.createBridge(chrome, fetcher, () => NOW) };
}

function expectError(code) {
  return (error) => error instanceof bridge.BridgeError && error.code === code;
}

test("upload extension has no cookie permission and embeds its connector only on approved publisher origins", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(extensionDir, "manifest.json"), "utf8"));
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.permissions, ["storage", "alarms", "tabs", "debugger"]);
  assert.equal(manifest.optional_permissions, undefined);
  assert.equal(manifest.background.service_worker, "upload-worker.js");
  assert.deepEqual(manifest.host_permissions, ["http://127.0.0.1:8765/*"]);
  assert.equal(manifest.optional_host_permissions, undefined);
  assert.deepEqual(manifest.content_scripts, [{matches:["http://127.0.0.1:8765/*"],js:["page-bridge.js"],run_at:"document_idle",all_frames:false}]);
  assert.equal(manifest.incognito, "not_allowed");
  for (const key of ["externally_connectable", "web_accessible_resources"]) assert.equal(manifest[key], undefined);
  assert.match(manifest.content_security_policy.extension_pages, /connect-src http:\/\/127\.0\.0\.1:8765 ws:\/\/127\.0\.0\.1:\*$/);
});

test("platform matching uses HTTPS and dot boundaries, never deceptive suffixes or broad Google domains", () => {
  for (const url of ["https://creator.douyin.com/", "https://www.douyin.com/"]) assert.equal(bridge.platformFromUrl(url).id, "douyin");
  for (const url of ["https://douyin.com.evil.test/", "https://evildouyin.com/", "https://douyin.com./", "https://douyin.com:444/", "http://douyin.com/", "https://user@douyin.com/", "https://google.com/", "https://accounts.google.com/", "https://channels.weixin.qq.com/", "https://bilibili.com/", "file:///douyin.com", "not a URL"]) assert.equal(bridge.platformFromUrl(url), null);
  assert.equal(bridge.domainMatches("www.douyin.com.evil.test", "douyin.com"), false);
  assert.equal(bridge.domainMatches(".douyin.com", "douyin.com"), false);
});

test("permission request runs synchronously inside the click call and requests one platform only", async () => {
  const f = fixture();
  const pending = f.client.syncFromClick(f.tab, CODE);
  assert.deepEqual(f.calls.map((call) => call.type), ["request"]);
  assert.deepEqual(f.calls[0].details, { origins: ["https://*.douyin.com/*"] });
  await pending;
  const types = f.calls.map((call) => call.type);
  assert.ok(types.indexOf("handshake") < types.indexOf("stores"));
  assert.ok(types.indexOf("handshake") < types.indexOf("script"));
  assert.ok(types.indexOf("handshake") < types.indexOf("cookies"));
  assert.equal(types.at(-1), "remove");
});

for (const [platform, url] of [
  ["douyin", "https://creator.douyin.com/home?synthetic=private#hash"],
  ["xiaohongshu", "https://creator.xiaohongshu.com/publish/publish"],
  ["youtube", `https://studio.youtube.com/channel/${CHANNEL}/videos/upload?synthetic=private`]
]) {
  test(`${platform}: synthetic session respects store, main frame, payload and local destination`, async () => {
    const f = fixture({ tab: { url } });
    const result = await f.client.syncFromClick(f.tab, CODE);
    assert.deepEqual(result, { platform, name: bridge.PLATFORMS[platform].name, cookieCount: 1, jobId: "test-job" });
    const query = f.calls.find((call) => call.type === "cookies");
    assert.deepEqual(query.details, { domain: bridge.PLATFORMS[platform].domain, storeId: f.storeId });
    const script = f.calls.find((call) => call.type === "script").details;
    assert.deepEqual(script.target, { tabId: 17, frameIds: [0] });
    assert.equal(script.world, "ISOLATED");
    assert.deepEqual(script.args, [new URL(url).origin]);
    const handshake = f.calls.find((call) => call.type === "handshake");
    assert.deepEqual(handshake.body, { code: CODE });
    const sent = f.calls.find((call) => call.type === "import");
    assert.equal(sent.url, "http://127.0.0.1:8765/api/browser/import");
    assert.equal(sent.init.headers["X-Bridge-Code"], CODE);
    assert.equal(sent.init.redirect, "error");
    assert.equal(sent.init.credentials, "omit");
    assert.equal(sent.init.cache, "no-store");
    assert.equal(sent.body.platform, platform);
    assert.equal(sent.body.page_url, new URL(url).origin + new URL(url).pathname);
    assert.equal(sent.body.account_hint, "");
    assert.equal(sent.body.cookies[0].value, "synthetic-cookie-not-real");
    assert.equal(sent.body.cookies[0].storeId, undefined);
    assert.equal(sent.body.code, undefined);
    assert.deepEqual(sent.body.origins, [{ origin: new URL(url).origin, localStorage: [{ name: "synthetic_state", value: "synthetic-local-storage-not-real" }] }]);
    assert.equal(JSON.stringify(result).includes("synthetic-cookie"), false);
    assert.equal(JSON.stringify(result).includes(CODE), false);
  });
}

test("invalid code and incognito tabs fail before requesting permissions or any session reads", async () => {
  for (const [tab, code, error] of [[{}, "no", "invalid_code"], [{ incognito: true }, CODE, "incognito"], [{ url: "https://www.bilibili.com/" }, CODE, "unsupported"], [{ url: "https://login.douyin.com/" }, CODE, "unsupported"]]) {
    const f = fixture({ tab });
    await assert.rejects(f.client.syncFromClick(f.tab, code), expectError(error));
    assert.deepEqual(f.calls, []);
  }
});

test("YouTube preserves one existing numeric authuser and drops all other URL context", async () => {
  for (const authuser of [0, 1, 99]) {
    const f = fixture({ tab: { url: `https://studio.youtube.com/channel/${CHANNEL}/videos?private=discard&authuser=${authuser}#discard-fragment` } });
    await f.client.syncFromClick(f.tab, CODE);
    const sent = f.calls.find((call) => call.type === "import").body;
    assert.equal(sent.page_url, `https://studio.youtube.com/channel/${CHANNEL}/videos?authuser=${authuser}`);
    assert.equal(JSON.stringify(sent).includes("discard"), false);
  }
});

test("invalid or duplicate YouTube authuser fails before permission or credential reads", async () => {
  for (const query of ["authuser=", "authuser=-1", "authuser=100", "authuser=01", "authuser=1.0", "authuser=+1", "authuser=%EF%BC%91", "authuser=private%40example.invalid", "authuser=%001", "authuser=1&authuser=1", "authuser=1&%61uthuser=2"]) {
    const f = fixture({ tab: { url: `https://studio.youtube.com/channel/${CHANNEL}?${query}` } });
    await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("youtube_context"));
    assert.deepEqual(f.calls, []);
  }
  assert.match(bridge.MESSAGES.youtube_context, /重新打开目标 Studio/);
  assert.equal(bridge.MESSAGES.youtube_context.includes("example.invalid"), false);
});

test("YouTube account selection changes during sync are blocked even on the same channel", async () => {
  for (const after of ["", "?authuser=2"]) {
    const base = `https://studio.youtube.com/channel/${CHANNEL}`;
    const f = fixture({ tab: { url: base + "?authuser=1" }, getTab: (_, tab) => ({ ...tab, url: base + after }) });
    await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("tab_changed"));
    assert.equal(f.calls.some((call) => call.type === "cookies" || call.type === "import"), false);
  }
});

test("other platforms strip authuser without interpreting it", async () => {
  for (const domain of ["douyin.com", "xiaohongshu.com"]) {
    const f = fixture({ tab: { url: `https://creator.${domain}/home?authuser=private%40example.invalid&authuser=1` } });
    await f.client.syncFromClick(f.tab, CODE);
    assert.equal(f.calls.find((call) => call.type === "import").body.page_url, `https://creator.${domain}/home`);
  }
});

test("YouTube must have the active Studio channel path before any permission or session access", async () => {
  for (const url of ["https://www.youtube.com/", "https://studio.youtube.com/", "https://studio.youtube.com/channel/UCshort/videos", `https://studio.youtube.com/channel/${CHANNEL}extra/videos`]) {
    const f = fixture({ tab: { url } });
    await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("youtube_channel"));
    assert.deepEqual(f.calls, []);
  }
});

test("denied permission does not call handshake or read session data", async () => {
  const f = fixture({ permission: false });
  await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("permission_denied"));
  assert.deepEqual(f.calls.map((call) => call.type), ["request"]);
});

test("wrong platform pairing stops before any cookie or localStorage access", async () => {
  const f = fixture({ handshake: { platform: "xiaohongshu" } });
  await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("platform_mismatch"));
  assert.deepEqual(f.calls.map((call) => call.type), ["request", "handshake", "remove"]);
});

test("expired, unknown or malformed pairing response stops before session reads", async () => {
  for (const handshake of [{ expires_at: "2026-10-04T11:00:00Z" }, { expires_at: null }, { platform: "bilibili" }, { platform: "toString" }]) {
    const f = fixture({ handshake });
    await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("invalid_handshake"));
    assert.deepEqual(f.calls.map((call) => call.type), ["request", "handshake", "remove"]);
  }
});

test("missing or ambiguous current CookieStore never falls back to the default store", async () => {
  for (const stores of [[{ id: "0", tabIds: [1] }], [{ id: "0", tabIds: [17] }, { id: "1", tabIds: [17] }]]) {
    const f = fixture({ stores });
    await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("store_unavailable"));
    assert.equal(f.calls.some((call) => ["script", "cookies", "import"].includes(call.type)), false);
  }
  const f = fixture({ tab: { storeId: "wrong-store" } });
  await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("store_unavailable"));
});

test("foreign or deceptive Cookie domains and wrong CookieStores cancel the entire import", async () => {
  const base = fixture().cookie;
  for (const patch of [{ domain: ".google.com" }, { domain: ".douyin.com.evil.test" }, { domain: ".evildouyin.com" }, { domain: "..douyin.com" }, { domain: "douyin.com/" }, { storeId: "another-profile" }]) {
    const f = fixture({ cookies: [base, { ...base, ...patch }] });
    await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("cookie_scope"));
    assert.equal(f.calls.some((call) => call.type === "import"), false);
  }
});

test("partitioned sessions fail explicitly rather than silently dropping cookies", async () => {
  const base = fixture().cookie;
  for (const patch of [{ partitionKey: { topLevelSite: "https://douyin.com" } }, { partitioned: true }]) {
    const f = fixture({ cookies: [base, { ...base, ...patch }] });
    await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("partitioned_session"));
    assert.equal(f.calls.some((call) => call.type === "import"), false);
  }
});

test("empty sessions and oversized cookie payloads are not submitted", async () => {
  for (const [cookies, error] of [[[], "empty_session"], [[{ ...fixture().cookie, value: "x".repeat(1024 * 1024 + 1) }], "too_large"]]) {
    const f = fixture({ cookies });
    await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError(error));
    assert.equal(f.calls.some((call) => call.type === "import"), false);
  }
});

test("unexpected origin or iframe result cannot be included in the import", async () => {
  for (const scriptResults of [[{ frameId: 0, result: { origin: "https://accounts.google.com", localStorage: [] } }], [{ frameId: 2, result: { origin: "https://creator.douyin.com", localStorage: [] } }]]) {
    const f = fixture({ scriptResults });
    await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("storage_unavailable"));
    assert.equal(f.calls.some((call) => ["cookies", "import"].includes(call.type)), false);
  }
});

test("tab navigation and channel switching abort before importing stale session data", async () => {
  const f = fixture({ getTab: (read, tab) => read >= 2 ? { ...tab, url: "https://www.douyin.com/" } : tab });
  await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("tab_changed"));
  assert.equal(f.calls.some((call) => ["cookies", "import"].includes(call.type)), false);
  const y = fixture({ tab: { url: `https://studio.youtube.com/channel/${CHANNEL}/videos` }, getTab: (read, tab) => ({ ...tab, url: `https://studio.youtube.com/channel/UC${"B".repeat(22)}/videos` }) });
  await assert.rejects(y.client.syncFromClick(y.tab, CODE), expectError("tab_changed"));
  assert.equal(y.calls.some((call) => ["stores", "script", "cookies", "import"].includes(call.type)), false);
});

test("import acknowledgement must match requested platform and include a validation job", async () => {
  for (const importResult of [{ accepted: false }, { platform: "youtube" }, { job: null }]) {
    const f = fixture({ importResult });
    await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("import_failed"));
    assert.equal(f.calls.at(-1).type, "remove");
  }
});

test("network and HTTP errors are sanitized and platform grants are removed", async () => {
  const f = fixture({ fetchError: "synthetic-sensitive-data-should-not-surface" });
  await assert.rejects(f.client.syncFromClick(f.tab, CODE), (error) => error.code === "unavailable" && !error.message.includes("synthetic-sensitive"));
  assert.equal(f.calls.at(-1).type, "remove");
  for (const options of [{ httpError: "handshake" }, { invalidJson: true }]) {
    const failed = fixture(options);
    await assert.rejects(failed.client.syncFromClick(failed.tab, CODE), expectError("invalid_handshake"));
    assert.equal(failed.calls.some((call) => call.type === "cookies"), false);
  }
});

test("concurrent clicks are rejected, but a completed attempt does not lock out later user clicks", async () => {
  const f = fixture();
  const first = f.client.syncFromClick(f.tab, CODE);
  await assert.rejects(f.client.syncFromClick(f.tab, CODE), expectError("busy"));
  await first;
  await f.client.syncFromClick(f.tab, CODE);
  assert.equal(f.calls.filter((call) => call.type === "request").length, 2);
});

test("serialized storage reader checks the origin before touching localStorage", () => {
  let touched = false;
  const context = {
    location: { protocol: "https:", origin: "https://accounts.google.com" }, window: {},
    get localStorage() { touched = true; throw new Error("Must not read"); }
  };
  context.window.top = context.window;
  const result = vm.runInNewContext(`(${bridge.readCurrentOriginStorage.toString()})("https://creator.douyin.com")`, context);
  assert.equal(result.failure, "tab_changed");
  assert.equal(touched, false);
});

test("serialized storage reader reads only bounded name/value pairs and handles denied storage", () => {
  const context = { TextEncoder, location: { protocol: "https:", origin: "https://creator.douyin.com" }, window: {}, localStorage: { length: 1, key: () => "synthetic_key", getItem: () => "synthetic_value" } };
  context.window.top = context.window;
  const invoke = () => vm.runInNewContext(`(${bridge.readCurrentOriginStorage.toString()})("https://creator.douyin.com")`, context);
  assert.equal(JSON.stringify(invoke()), JSON.stringify({ origin: "https://creator.douyin.com", localStorage: [{ name: "synthetic_key", value: "synthetic_value" }] }));
  context.localStorage = { length: 257 };
  assert.equal(invoke().failure, "too_large");
  context.localStorage = { length: 1, key: () => "synthetic_key", getItem: () => "汉".repeat(50000) };
  assert.equal(invoke().failure, "too_large");
  context.localStorage = { length: 1, key() { throw new Error("synthetic exception"); } };
  assert.equal(invoke().failure, "storage_unavailable");
});

test("opening popup only checks the active tab; success clears code and shows counts, never credentials", async () => {
  const f = fixture();
  const elements = Object.fromEntries(["code", "sync", "status", "platform"].map((id) => [id, { value: "", disabled: id === "sync", textContent: "", dataset: {}, handlers: {}, addEventListener(type, fn) { this.handlers[type] = fn; } }]));
  const popupApi = { ...bridge, createBridge: () => f.client };
  vm.runInNewContext(fs.readFileSync(path.join(extensionDir, "popup.js"), "utf8"), {
    document: { getElementById: (id) => elements[id] }, LoginBridge: popupApi, chrome: f.chrome, fetch: f.fetcher
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(f.calls.map((call) => call.type), ["query"]);
  elements.code.value = CODE;
  elements.code.handlers.input();
  assert.equal(elements.sync.disabled, false);
  elements.sync.handlers.click();
  assert.equal(f.calls.at(-1).type, "request");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(elements.code.value, "");
  assert.equal(elements.sync.disabled, true);
  assert.match(elements.status.textContent, /已同步 1 个 Cookie/);
  assert.match(elements.status.textContent, /发布台正在验证/);
  assert.equal(elements.status.textContent.includes("synthetic-cookie"), false);
  assert.equal(elements.status.textContent.includes(CODE), false);
});

test("all three synthetic extension payloads are accepted by the real server session-format validator", async () => {
  const payloads = [];
  for (const url of ["https://creator.douyin.com/home", "https://creator.xiaohongshu.com/publish", `https://studio.youtube.com/channel/${CHANNEL}/videos?authuser=2&private=discard`]) {
    const f = fixture({ tab: { url } });
    await f.client.syncFromClick(f.tab, CODE);
    payloads.push(f.calls.find((call) => call.type === "import").body);
  }
  const python = [
    "import json, sys, tempfile",
    "from pathlib import Path",
    "import browser_sessions",
    "for payload in json.load(sys.stdin):",
    "    with tempfile.TemporaryDirectory(prefix='synthetic-extension-test-') as directory:",
    "        metadata = browser_sessions.validate_and_save(payload['platform'], payload, Path(directory), Path(directory))",
    "        assert metadata['cookie_count'] == 1",
    "        assert metadata['source'] == 'chrome'",
    "        if payload['platform'] == 'youtube':",
    "            assert metadata['channel_id'] == 'UC' + 'A' * 22",
    "            assert metadata['authuser'] == 2",
    "            saved = json.loads((Path(directory) / 'youtube_default.json.publisher.json').read_text())",
    "            assert saved['authuser'] == 2 and len(saved['context_sha256']) == 64",
    "print('synthetic format validation: 3 platforms accepted')"
  ].join("\n");
  const checked = spawnSync("python3", ["-c", python], { cwd: path.resolve(__dirname, ".."), input: JSON.stringify(payloads), encoding: "utf8" });
  assert.equal(checked.status, 0, "Server session-format validator must accept extension payloads; no real browser validation is attempted.");
  assert.equal(checked.stdout.trim(), "synthetic format validation: 3 platforms accepted");
});
