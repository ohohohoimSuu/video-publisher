const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
const initializers = source.indexOf('\n  renderPlatforms();\n  $("#publish-form")');
assert.ok(initializers > 0);
function element(extra = {}) {
  return { value: '', textContent: '', innerHTML: '', hidden: false, disabled: false, dataset: {},
    classList: { toggle() {}, add() {}, remove() {} }, focus() { this.focused = true; }, getAttribute(name) { return this[name] || null; }, scrollIntoView() {}, after() {}, showModal() { this.open = true; }, close() { this.open = false; },
    removeAttribute(name) { delete this[name]; }, setAttribute(name, value) { this[name] = value; }, ...extra };
}
function harness({ origin = 'http://127.0.0.1:8765', backend } = {}) {
  const singles = new Map(), multiples = new Map(), requests = [], storage = new Map(), localStorage = new Map();
  const document = { hidden: false,
    querySelector(s) { if (!singles.has(s)) singles.set(s, element()); return singles.get(s); },
    querySelectorAll(s) { return multiples.get(s) || []; }, createElement() { return element(); } };
  class XHR {
    constructor() { this.listeners = {}; this.upload = { addEventListener() {} }; requests.push(this); }
    open(method, url) { this.method = method; this.url = url; } setRequestHeader() {} send() {}
    addEventListener(name, handler) { this.listeners[name] = handler; }
    succeed(asset) { this.status = 201; this.response = { asset }; this.listeners.load(); }
    fail() { this.listeners.error(); }
  }
  const context = { URL, console, document, XMLHttpRequest: XHR, FormData: class { append() {} },
    window: { PUBLISHER_LOCAL_BACKEND: backend, location: { origin }, confirm: () => true, localStorage: { getItem: (key) => localStorage.get(key) || null, setItem: (key, value) => localStorage.set(key, value), removeItem: (key) => localStorage.delete(key) }, sessionStorage: { getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) } },
    setTimeout: () => 1, clearTimeout() {}, fetch: async () => { throw new Error('Unexpected fetch'); } };
  vm.createContext(context);
  vm.runInContext(source.slice(0, initializers) + `
    this.app = {api, apiURL, model, showView, renderAccounts, safeLink, assetLink, collectPayload, renderAsset, renderPlatforms, updatePlatformState,
      updateActions, uploadFile, validate, publish, refreshState, upsertJob, poll, accountInfo, supportsChrome,
      connectChrome, pollBrowserPair, closeBrowserPair, copyBrowserCode, accountAction, renderRuntime,
      captureDraft, saveDraft, readSessionDraft, restoreSessionDraft, restoreJobDraft, draftChanged, clearDraft,
      renderJobs, renderJobDetails, resultProgress, canRetry, resolveResult, canResumeCover, resumeCover,
      readTemplates, saveTemplate, applyTemplate, deleteTemplate, renderTemplates, openRecentAssets, useRecentAsset, closeAssetPicker, editorIssues, focusField, assetSummary, videoPreviewUnavailable, removeAsset};
    this.hook = (h) => {
      if (h.collectPayload) collectPayload = h.collectPayload;
      if (h.localErrors) localErrors = h.localErrors;
      if (h.showValidation) showValidation = h.showValidation;
      if (h.toast) toast = h.toast;
      if (h.renderPlatforms) renderPlatforms = h.renderPlatforms;
      if (h.renderRuntime) renderRuntime = h.renderRuntime;
      if (h.updatePlatformState) updatePlatformState = h.updatePlatformState;
      if (h.renderJobs) renderJobs = h.renderJobs;
      if (h.refreshState) refreshState = h.refreshState;
    };
  })();`, context);
  context.app.model.token = 'test-token'; context.app.model.runtime = { ready: true };
  // Generic regression fixtures support both platforms; dedicated mode tests below
  // verify the production API's current Douyin-only allowlist.
  context.app.model.preparePlatforms = ['douyin', 'youtube'];
  function zone(slot) {
    const children = new Map();
    for (const s of ['.upload-error', '.upload-progress', 'progress', '.progress-number', '.progress-caption', '.file-input', '.upload-placeholder', '.asset-preview', '.asset-name', '.asset-meta', 'img', 'video', '.video-file-icon', '.video-preview-notice']) children.set(s, element());
    const buttons = [element(), element()];
    singles.set(`.upload-zone[data-slot="${slot}"]`, element({ querySelector: (s) => children.get(s), querySelectorAll: () => buttons }));
    return { children, buttons };
  }
  return { context, app: context.app, hook: context.hook, singles, multiples, document, requests, storage, localStorage, zone };
}
const response = (data) => ({ ok: true, status: 200, json: async () => data });
function deferred() { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; }
test('hosted API calls use only the fixed loopback backend and keep tokens local', async () => {
  const h = harness({origin:'https://publisher.example.workers.dev', backend:'http://127.0.0.1:8765'});
  let call; h.context.fetch=async(url,options)=>{call={url,options};return response({valid:false});};
  await h.app.api('/api/validate',{method:'POST',body:{title:'private draft'}});
  assert.equal(call.url,'http://127.0.0.1:8765/api/validate');
  assert.equal(call.options.credentials,'omit'); assert.equal(call.options.mode,'cors');
  assert.equal(call.options.targetAddressSpace,'loopback');
  assert.equal(call.options.headers['X-Publisher-Token'],'test-token');
  assert.equal(h.app.assetLink('/api/assets/video-id'),'http://127.0.0.1:8765/api/assets/video-id');
  assert.equal(h.app.assetLink('https://publisher.example.workers.dev/api/assets/video-id'),null);
});
test('hosted uploads send files straight to the local backend', async () => {
  const h = harness({origin:'https://publisher.example.workers.dev', backend:'http://127.0.0.1:8765'}); h.zone('video');
  const upload=h.app.uploadFile('video',{name:'test.mp4',size:200*1024*1024});
  assert.equal(h.requests[0].url,'http://127.0.0.1:8765/api/assets');
  h.requests[0].succeed({id:'local-video',url:'/api/assets/local-video'}); await upload;
  assert.equal(h.app.model.assets.video.id,'local-video');
});
test('untrusted configured backend cannot receive local tokens or files', async () => {
  const h=harness({origin:'https://publisher.example.workers.dev',backend:'https://foreign.example'});
  let url;h.context.fetch=async(value)=>{url=value;return response({});};
  await h.app.api('/api/state'); assert.equal(url,'/api/state');
});
test('hosted connection failure explains service and browser permission', async () => {
  const h=harness({origin:'https://publisher.example.workers.dev',backend:'http://127.0.0.1:8765'});
  h.context.fetch=async()=>{throw new TypeError('Failed to fetch');};
  await assert.rejects(h.app.api('/api/state'),/启动发布台.*允许浏览器访问本机服务/);
});
test('result links reject malformed values; assets accept only local asset paths', () => {
  const { app } = harness();
  for (const v of [undefined, null, '', 'undefined', '/undefined', 'javascript:alert(1)', 'data:text/html,hi']) assert.equal(app.safeLink(v), null);
  assert.equal(app.safeLink('https://www.youtube.com/watch?v=example'), 'https://www.youtube.com/watch?v=example');
  assert.equal(app.assetLink('/api/assets/abc-123'), 'http://127.0.0.1:8765/api/assets/abc-123');
  for (const v of [undefined, null, '', '/undefined', '//evil.test/api/assets/id', 'https://evil.test/api/assets/id', '/api/assets/id?x=1', '/api/assets/id#x', '/api/assets/a/b', 'javascript:alert(1)']) assert.equal(app.assetLink(v), null);
});
test('uploaded cover gets a working preview URL', () => {
  const h = harness(), z = h.zone('cover');
  h.app.model.assets.cover = { id: 'abc123', url: '/api/assets/abc123', name: 'cover.png', size: 12 }; h.app.renderAsset('cover');
  assert.equal(z.children.get('img').src, 'http://127.0.0.1:8765/api/assets/abc123'); assert.equal(z.children.get('.asset-preview').hidden, false);
});
test('common and platform descriptions keep their whitespace', () => {
  const h = harness(); h.multiples.set("input[name='platform']:checked:not(:disabled)", [{ value: 'douyin' }]);
  h.document.querySelector('#description').value = '\n Common text \n\n'; h.document.querySelector('#douyin-description').value = '\n Platform text \n';
  const p = h.app.collectPayload(); assert.equal(p.description, '\n Common text \n\n'); assert.equal(p.overrides.douyin.description, '\n Platform text \n');
});
test('capability text exposes unsupported descriptions, tags and notes', () => {
  const h = harness(); h.app.model.platforms = [{ id: 'baijiahao', capabilities: { description: false, tags: false, note: '仅填写标题和封面' } }]; h.app.updatePlatformState();
  const t = h.document.querySelector('[data-capabilities="baijiahao"]').textContent; assert.match(t, /不支持正文/); assert.match(t, /不支持标签/); assert.match(t, /仅填写标题和封面/);
});
test('tag delimiters preserve C# and remove only leading hash marks', () => {
  const h = harness(); h.document.querySelector('#tags').value = 'C#，#coding、##开发\nC#';
  assert.equal(JSON.stringify(h.app.collectPayload().tags), JSON.stringify(['C#', 'coding', '开发']));
});
test('pasted hash-prefixed topics become separate tags without damaging C#', () => {
  const h = harness(); h.document.querySelector('#tags').value = '#混沌  #蝴蝶效应  #三体  #科普  #数学之美  #物理';
  assert.equal(JSON.stringify(h.app.collectPayload().tags), JSON.stringify(['混沌','蝴蝶效应','三体','科普','数学之美','物理']));
  h.document.querySelector('#tags').value = 'C# #编程，machine learning\n#编程';
  assert.equal(JSON.stringify(h.app.collectPayload().tags), JSON.stringify(['C#','编程','machine learning']));
});
test('parallel uploads finish independently; same-slot replacement gives feedback', async () => {
  const h = harness(), inputs = composer(h, ['douyin']); inputs[0].checked = true; h.document.querySelector('#title').value = 'Title'; h.document.querySelector('#ai-generated').value = 'false'; const z = h.zone('video'); h.zone('cover'); const notices = []; h.hook({ toast: (t) => notices.push(t) });
  const one = h.app.uploadFile('video', { name: 'one.mp4', type: 'video/mp4' }); const two = h.app.uploadFile('cover', { name: 'cover.png', type: 'image/png' });
  await h.app.uploadFile('video', { name: 'two.mp4', type: 'video/mp4' });
  assert.equal(h.requests.length, 2); assert.equal(h.app.model.uploading.size, 2); assert.equal(z.children.get('.file-input').disabled, true); assert.equal(h.document.querySelector('#publish-button').disabled, true); assert.match(notices[0], /等待完成/);
  h.requests[1].succeed({ id: 'cover', url: '/api/assets/cover' }); await two; assert.equal(h.app.model.uploading.size, 1); assert.equal(h.document.querySelector('#publish-button').disabled, true);
  h.requests[0].succeed({ id: 'video' }); await one; assert.equal(h.app.model.uploading.size, 0); assert.equal(h.app.model.assets.video.id, 'video'); assert.equal(h.app.model.assets.cover.id, 'cover'); assert.equal(z.children.get('.file-input').disabled, false); assert.equal(h.document.querySelector('#publish-button').disabled, false);
});
test('failed replacement retains previous asset and explains that state', async () => {
  const h = harness(), z = h.zone('cover'); h.app.model.assets.cover = { id: 'previous' };
  const upload = h.app.uploadFile('cover', { name: 'new.png', type: 'image/png' }); h.requests[0].fail(); await upload;
  assert.equal(h.app.model.assets.cover.id, 'previous'); assert.match(z.children.get('.upload-error').textContent, /仍保留之前的文件/); assert.equal(z.children.get('.upload-error').hidden, false); assert.equal(h.app.model.uploading.size, 0);
});
test('validation ignores double clicks and stale success after editing', async () => {
  const h = harness(), d = deferred(); let requests = 0, title = 'before'; const panels = [], notices = [];
  h.context.fetch = () => { requests += 1; return d.promise; }; h.hook({ collectPayload: () => ({ title }), showValidation: (v) => panels.push(v), toast: (t) => notices.push(t) });
  const first = h.app.validate(); await h.app.validate(); assert.equal(requests, 1); title = 'after'; d.resolve(response({ valid: true })); await first;
  assert.equal(panels.length, 0); assert.match(notices[0], /材料已修改/); assert.equal(h.app.model.validating, false);
});
test('double publish creates one request and retains composer content', async () => {
  const h = harness(), d = deferred(); let requests = 0, confirms = 0; h.context.fetch = () => { requests++; return d.promise; }; h.context.window.confirm = () => { confirms++; return true; };
  const payload = { title: 'untouched', description: '\nkeep\n', platforms: ['douyin'], overrides: {} }; h.hook({ collectPayload: () => payload, localErrors: () => [], renderJobs() {}, refreshState: async () => {} });
  const first = h.app.publish({ preventDefault() {} }); await h.app.publish({ preventDefault() {} }); assert.equal(requests, 1); assert.equal(confirms, 1);
  d.resolve(response({ job: { id: 'new-job', status: 'queued' } })); await first; assert.equal(payload.description, '\nkeep\n'); assert.equal(h.app.model.submitting, false);
});
test('stale state responses cannot delete new jobs or regress newer details', async () => {
  const h = harness(), d = deferred(); h.context.fetch = () => d.promise; h.hook({ renderPlatforms() {}, renderRuntime() {}, updatePlatformState() {}, renderJobs() {} });
  h.app.model.jobs = [{ id: 'old', status: 'running' }]; const refresh = h.app.refreshState(); h.app.upsertJob({ id: 'new', status: 'queued' }); h.app.upsertJob({ id: 'old', status: 'completed' });
  d.resolve(response({ token: 'next', jobs: [{ id: 'old', status: 'running' }] })); await refresh; assert.equal(h.app.model.jobs.length, 2); assert.equal(h.app.model.jobs.find((j) => j.id === 'old').status, 'completed'); assert.equal(h.app.model.jobs.find((j) => j.id === 'new').status, 'queued');
});
test('platform rerender preserves selections, descriptions, settings and expanded cards', () => {
  const h = harness(), selector = '#platform-list input, #platform-list textarea, #platform-list select';
  const before = [element({ value: 'youtube', type: 'checkbox', checked: true }), element({ id: 'youtube-description', value: '\nmy text\n' }), element({ id: 'youtube-kids', value: 'false' })];
  const after = [element({ value: 'youtube', type: 'checkbox', checked: false }), element({ id: 'youtube-description' }), element({ id: 'youtube-kids' })];
  const details = element({ open: false }), card = element({ dataset: { platform: 'youtube' }, querySelector: (s) => s === 'details' ? details : null });
  h.multiples.set(selector, before); h.multiples.set('.platform-card > details[open]', [{ closest: () => card }]); h.multiples.set('.platform-card', [card]);
  const list = element(); Object.defineProperty(list, 'innerHTML', { set() { h.multiples.set(selector, after); } }); h.singles.set('#platform-list', list); h.app.model.platforms = [{ id: 'youtube', name: 'YouTube' }]; h.app.renderPlatforms();
  assert.equal(after[0].checked, true); assert.equal(after[1].value, '\nmy text\n'); assert.equal(after[2].value, 'false'); assert.equal(details.open, true);
});
test('detail-only polling errors show alongside retained logs', async () => {
  const h = harness(); h.app.model.detailId = 'done'; h.document.querySelector('#job-dialog').open = true; h.singles.set('#dialog-refresh-error', null);
  let warning; h.document.querySelector('#dialog-content').prepend = (n) => { warning = n; }; h.context.fetch = async () => { throw new Error('offline'); }; await h.app.poll(); assert.match(warning.textContent, /上次读取的内容.*offline/);
});

test('Chrome connection is offered only for the three supported platforms', () => {
  const h = harness();
  h.app.model.browserBridge = { state: 'available', platforms: ['douyin', 'xiaohongshu', 'youtube', 'bilibili', 'tencent'] };
  h.app.model.platforms = ['douyin', 'xiaohongshu', 'youtube', 'bilibili', 'tencent'].map((id) => ({ id, name: id }));
  h.app.renderPlatforms();
  const html = h.document.querySelector('#account-list').innerHTML + h.document.querySelector('#unsupported-account-list').innerHTML;
  for (const id of ['douyin', 'xiaohongshu', 'youtube']) assert.match(html, new RegExp(`data-account-action="chrome" data-platform="${id}"`));
  for (const id of ['bilibili', 'tencent']) assert.doesNotMatch(html, new RegExp(`data-account-action="chrome" data-platform="${id}"`));
  assert.match(html, /App 扫码授权/); assert.match(html, /直接登录/);
});
test('imported sessions stay pending verification and Chrome status describes a snapshot', () => {
  const h = harness();
  for (const status of ['imported', 'pending_verification']) {
    assert.equal(h.app.accountInfo(status)[1], 'pending_verification');
    assert.match(h.app.accountInfo(status)[0], /待在线核验/);
  }
  assert.equal(h.app.accountInfo('not_connected')[0], '未连接');
  h.app.model.browserBridge = { state: 'available', platforms: ['douyin'] };
  h.app.model.platforms = [{ id: 'douyin', account_status: 'verified', account_source: 'chrome', imported_at: '2026-10-04T01:00:00Z', account_hint: 'My channel' }];
  h.app.updatePlatformState();
  assert.match(h.document.querySelector('[data-account-status="douyin"]').textContent, /登录已保存/);
  assert.match(h.document.querySelector('[data-account-context="douyin"]').textContent, /My channel.*登录已保存在本机/);
  h.app.model.platforms[0] = { id: 'douyin', account_status: 'not_connected' }; h.app.updatePlatformState();
  assert.match(h.document.querySelector('[data-account-context="douyin"]').textContent, /无需安装扩展/);
});
test('pair creation and polling keep the code in authenticated request bodies and never mark an import verified', async () => {
  const h = harness(), code = 'a'.repeat(64), calls = []; let refreshes = 0;
  h.app.model.browserBridge = { state: 'available', platforms: ['douyin'] };
  h.app.model.platforms = [{ id: 'douyin', account_status: 'not_connected' }];
  h.document.querySelector('#title').value = 'Keep my title'; h.document.querySelector('#douyin-description').value = '\nKeep my draft\n';
  h.context.fetch = async (path, options) => { calls.push({ path, options }); return response(path === '/api/browser/pair' ? { code, status: 'pending' } : { status: 'imported', job_id: 'verify-job' }); };
  h.hook({ refreshState: async () => { refreshes++; } });
  await h.app.connectChrome('douyin');
  assert.equal(h.document.querySelector('#browser-pair-dialog').open, true);
  assert.equal(h.document.querySelector('#browser-pair-code').textContent, code);
  await h.app.pollBrowserPair();
  assert.equal(calls.length, 2); assert.equal(calls[1].path, '/api/browser/status');
  for (const call of calls) { assert.equal(call.options.method, 'POST'); assert.equal(call.options.headers['X-Publisher-Token'], 'test-token'); assert.ok(!call.path.includes(code)); }
  assert.deepEqual(JSON.parse(calls[0].options.body), { platform: 'douyin' }); assert.deepEqual(JSON.parse(calls[1].options.body), { code });
  assert.equal(h.app.model.pairing.status, 'imported'); assert.equal(h.app.model.pairing.jobId, 'verify-job');
  assert.equal(h.app.model.platforms[0].account_status, 'not_connected'); assert.equal(refreshes, 1);
  assert.equal(h.document.querySelector('#browser-pair-code').textContent, '');
  assert.match(h.document.querySelector('#browser-pair-status').textContent, /正在在线核验.*不代表账号已验证/);
  assert.equal(h.document.querySelector('#title').value, 'Keep my title'); assert.equal(h.document.querySelector('#douyin-description').value, '\nKeep my draft\n');
  await h.app.closeBrowserPair(); assert.equal(calls.length, 2); assert.equal(h.app.model.pairing, null);
});
test('closing a pending dialog clears its code and cancels it with authentication', async () => {
  const h = harness(), code = 'b'.repeat(64), calls = [];
  h.app.model.pairing = { status: 'pending', code }; h.document.querySelector('#browser-pair-dialog').open = true; h.document.querySelector('#browser-pair-code').textContent = code;
  h.context.fetch = async (path, options) => { calls.push({ path, options }); return response({ status: 'cancelled' }); };
  await h.app.closeBrowserPair();
  assert.equal(h.app.model.pairing, null); assert.equal(h.document.querySelector('#browser-pair-dialog').open, false); assert.equal(h.document.querySelector('#browser-pair-code').textContent, '');
  assert.equal(calls[0].path, '/api/browser/cancel'); assert.equal(calls[0].options.headers['X-Publisher-Token'], 'test-token'); assert.deepEqual(JSON.parse(calls[0].options.body), { code });
});
test('closing while a pair is being created cancels the late code without reopening the dialog', async () => {
  const h = harness(), d = deferred(), code = 'c'.repeat(64), calls = [];
  h.app.model.browserBridge = { state: 'available', platforms: ['youtube'] };
  h.context.fetch = async (path, options) => { calls.push({ path, options }); return path === '/api/browser/pair' ? d.promise : response({ status: 'cancelled' }); };
  const create = h.app.connectChrome('youtube'); await h.app.closeBrowserPair(); d.resolve(response({ code })); await create;
  assert.equal(h.app.model.pairing, null); assert.equal(h.document.querySelector('#browser-pair-dialog').open, false); assert.equal(h.document.querySelector('#browser-pair-code').textContent, '');
  assert.equal(calls[1].path, '/api/browser/cancel'); assert.deepEqual(JSON.parse(calls[1].options.body), { code });
});
test('late pairing poll cannot overwrite a closed dialog and expiry is never treated as verification', async () => {
  const h = harness(), d = deferred(), code = 'd'.repeat(64);
  h.app.model.pairing = { platform: 'douyin', status: 'pending', code }; h.document.querySelector('#browser-pair-dialog').open = true;
  h.context.fetch = async (path) => path === '/api/browser/status' ? d.promise : response({ status: 'cancelled' });
  const poll = h.app.pollBrowserPair(); await h.app.closeBrowserPair(); d.resolve(response({ status: 'imported' })); await poll;
  assert.equal(h.app.model.pairing, null); assert.equal(h.document.querySelector('#browser-pair-code').textContent, '');
  h.app.model.pairing = { platform: 'douyin', status: 'pending', code }; h.document.querySelector('#browser-pair-dialog').open = true;
  h.context.fetch = async () => response({ status: 'expired' }); await h.app.pollBrowserPair();
  assert.equal(h.app.model.pairing.status, 'expired'); assert.equal(h.document.querySelector('#browser-pair-code').textContent, ''); assert.match(h.document.querySelector('#browser-pair-status').textContent, /已过期/);
});
test('import completion retries its state refresh after an older state request finishes', async () => {
  const h = harness(); let refreshes = 0;
  h.app.model.pairing = { platform: 'douyin', status: 'pending', code: 'e'.repeat(64) }; h.document.querySelector('#browser-pair-dialog').open = true;
  h.app.model.refreshing = true; h.context.fetch = async () => response({ status: 'imported', job_id: 'job' }); h.hook({ refreshState: async () => { refreshes++; } });
  await h.app.pollBrowserPair(); assert.equal(refreshes, 0); assert.equal(h.app.model.pairing.refreshNeeded, true);
  h.app.model.refreshing = false; await h.app.pollBrowserPair(); assert.equal(refreshes, 1); assert.equal(h.app.model.pairing.refreshNeeded, false);
});

function composer(h, ids = ['douyin', 'youtube']) {
  h.app.model.platforms = ids.map((id) => ({ id, name: id }));
  const inputs = ids.map((value) => element({ value, type: 'checkbox', checked: false }));
  const query = h.document.querySelectorAll;
  h.document.querySelectorAll = (selector) => selector === "input[name='platform']" ? inputs : selector === "input[name='platform']:checked:not(:disabled)" ? inputs.filter((input) => input.checked && !input.disabled) : query(selector);
  for (const slot of ['video', 'cover', 'landscape']) h.zone(slot);
  return inputs;
}
const material = () => ({
  payload: { title: 'Original title', description: '\nOriginal body\n\n', tags: ['C#', '创作'], ai_generated: false, video_id: 'big-video', cover_id: 'cover-id', landscape_cover_id: null, platforms: ['douyin', 'youtube'], overrides: { youtube: { title: 'YouTube title', description: '\nYT body\n' } }, youtube: { visibility: 'unlisted', made_for_kids: false, altered_synthetic: true } },
  assets: { video: { id: 'big-video', name: '440MB.mp4', size: 440 * 1024 * 1024, url: '/api/assets/big-video' }, cover: { id: 'cover-id', name: 'cover.png', size: 20, url: '/api/assets/cover-id' }, landscape: null }
});
test('session drafts whitelist form fields and asset IDs; no token, code, account or file bytes', () => {
  const h = harness(), inputs = composer(h); inputs[0].checked = true;
  h.app.model.token = 'TOKEN-SECRET'; h.app.model.pairing = { code: 'PAIR-SECRET' };
  h.app.model.platforms[0].cookies = 'COOKIE-SECRET';
  h.app.model.assets.video = { id: 'big-video', bytes: 'FILE-BYTES', path: '/private/asset', size: 440 * 1024 * 1024 };
  h.document.querySelector('#title').value = '  Keep spaces  ';
  h.document.querySelector('#description').value = '\nA paragraph\n\n';
  h.document.querySelector('#youtube-description').value = 'Unselected platform draft';
  h.app.saveDraft();
  const raw = [...h.storage.values()][0], draft = JSON.parse(raw);
  assert.equal(draft.fields.title, '  Keep spaces  '); assert.equal(draft.fields.description, '\nA paragraph\n\n');
  assert.equal(draft.fields['youtube-description'], 'Unselected platform draft');
  assert.deepEqual(draft.assets, { video: 'big-video' }); assert.deepEqual(draft.platforms, ['douyin']);
  for (const secret of ['TOKEN-SECRET', 'PAIR-SECRET', 'COOKIE-SECRET', 'FILE-BYTES', '/private/asset']) assert.ok(!raw.includes(secret));
  assert.match(h.document.querySelector('#draft-status').textContent, /已暂存.*刷新页面可恢复/);
});
test('session restore preserves text and selections using local asset info without uploading file bytes', async () => {
  const h = harness(), inputs = composer(h); const data = material(), calls = [];
  h.app.model.sessionDraft = { version: 1, fields: { title: '  Saved title  ', description: '\nSaved body\n', tags: '#C# #创作', 'ai-generated': 'false', 'youtube-kids': 'false', 'youtube-visibility': 'unlisted', 'youtube-description': '\nPer platform\n' }, platforms: ['youtube'], assets: { video: 'big-video', cover: 'cover-id' } };
  h.context.fetch = async (path, options) => { calls.push({ path, options }); return response({ asset: path.includes('big-video') ? data.assets.video : data.assets.cover }); };
  await h.app.restoreSessionDraft();
  assert.equal(h.document.querySelector('#title').value, '  Saved title  '); assert.equal(h.document.querySelector('#description').value, '\nSaved body\n');
  assert.equal(h.document.querySelector('#youtube-description').value, '\nPer platform\n');
  assert.equal(h.document.querySelector('#youtube-kids').value, 'false'); assert.equal(inputs[0].checked, false); assert.equal(inputs[1].checked, true);
  assert.equal(h.app.model.assets.video.id, 'big-video'); assert.equal(h.app.model.assets.cover.url, '/api/assets/cover-id');
  assert.deepEqual(calls.map((call) => call.path).sort(), ['/api/assets/big-video/info', '/api/assets/cover-id/info']);
  assert.ok(calls.every((call) => call.options.method === 'GET')); assert.equal(h.requests.length, 0);
});
test('missing session asset references are cleared and explained; surviving assets are reused', async () => {
  const h = harness(); composer(h);
  h.app.model.sessionDraft = { version: 1, fields: { title: 'Keep title' }, platforms: ['douyin'], assets: { video: 'missing-video', cover: 'cover-id' } };
  h.context.fetch = async (path) => path.includes('missing-video') ? { ok: false, status: 404, json: async () => ({ error: 'not found' }) } : response({ asset: material().assets.cover });
  await h.app.restoreSessionDraft();
  assert.equal(h.document.querySelector('#title').value, 'Keep title'); assert.equal(h.app.model.assets.video, undefined); assert.equal(h.app.model.assets.cover.id, 'cover-id');
  assert.match(h.document.querySelector('#draft-status').textContent, /视频.*不存在.*已移除引用/);
  assert.deepEqual(JSON.parse([...h.storage.values()][0]).assets, { cover: 'cover-id' }); assert.equal(h.requests.length, 0);
});
test('transient session asset failures preserve the saved draft and can retry on the next state check', async () => {
  const h = harness(); composer(h); const draft = { version: 1, fields: { title: 'Still saved' }, platforms: [], assets: { video: 'big-video' } };
  h.storage.set('video-publisher.draft.v1', JSON.stringify(draft)); h.app.model.sessionDraft = h.app.readSessionDraft();
  h.context.fetch = async () => { throw new Error('offline'); }; await h.app.restoreSessionDraft();
  assert.equal(h.app.model.sessionRestoreDone, false); assert.equal(JSON.parse([...h.storage.values()][0]).assets.video, 'big-video');
  assert.match(h.document.querySelector('#draft-status').textContent, /仍已保存.*重新检查/);
  h.context.fetch = async () => response({ asset: material().assets.video }); await h.app.restoreSessionDraft();
  assert.equal(h.document.querySelector('#title').value, 'Still saved'); assert.equal(h.app.model.assets.video.id, 'big-video');
});
test('session restoration never overwrites input edited while asset info is pending', async () => {
  const h = harness(), d = deferred(); composer(h);
  h.app.model.sessionDraft = { version: 1, fields: { title: 'Old title' }, platforms: ['youtube'], assets: { video: 'big-video' } };
  h.context.fetch = () => d.promise; const restore = h.app.restoreSessionDraft();
  h.document.querySelector('#title').value = 'New typing'; h.app.draftChanged();
  d.resolve(response({ asset: material().assets.video })); await restore;
  assert.equal(h.document.querySelector('#title').value, 'New typing'); assert.equal(h.app.model.assets.video, undefined); assert.equal(h.app.model.restoring, false);
});
test('safe platform retry restores original materials and selects only that platform without any POST', async () => {
  const h = harness(), inputs = composer(h), calls = [];
  h.app.model.jobs = [{ id: 'job', kind: 'publish', status: 'partial', results: [{ platform: 'douyin', status: 'submitted' }, { platform: 'youtube', status: 'failed', retry_safe: true }] }];
  h.context.fetch = async (path, options) => { calls.push({ path, options }); return response(material()); };
  await h.app.restoreJobDraft('job', 'youtube');
  assert.deepEqual(calls.map((call) => [call.path, call.options.method]), [['/api/jobs/job/draft', 'GET']]);
  assert.equal(h.requests.length, 0); assert.equal(inputs[0].checked, false); assert.equal(inputs[1].checked, true);
  assert.equal(h.document.querySelector('#title').value, 'Original title'); assert.equal(h.document.querySelector('#description').value, '\nOriginal body\n\n');
  assert.equal(h.document.querySelector('#youtube-description').value, '\nYT body\n'); assert.equal(h.document.querySelector('#youtube-visibility').value, 'unlisted');
  assert.equal(h.app.model.assets.video.id, 'big-video'); assert.equal(h.app.model.assets.cover.url, '/api/assets/cover-id');
  assert.match(h.document.querySelector('#recovery-notice').textContent, /仅选择youtube.*点击.*才会开始重试/);
});
test('failed and unstarted skipped results require strict retry_safe true; uncertain states never expose retry', () => {
  const h = harness();
  for (const [status, retry_safe, allowed] of [['failed', true, true], ['skipped', true, true], ['failed', false, false], ['failed', 'true', false], ['skipped', undefined, false], ['submitted', true, false], ['needs_attention', true, false], ['interrupted', true, false]]) {
    h.app.renderJobDetails({ id: 'job', kind: 'publish', status, results: [{ platform: 'youtube', status, retry_safe }] });
    const html = h.document.querySelector('#dialog-content').innerHTML;
    assert.equal(html.includes('data-retry-platform='), allowed, `${status} ${retry_safe}`);
    assert.ok(html.includes('恢复材料'));
    if (['needs_attention', 'interrupted'].includes(status)) assert.match(html, /待核验，不自动重试/);
  }
});
test('unsafe retry invocation is rejected before it fetches materials', async () => {
  const h = harness(), notices = []; composer(h); h.hook({ toast: (text) => notices.push(text) });
  h.app.model.jobs = [{ id: 'job', kind: 'publish', results: [{ platform: 'youtube', status: 'needs_attention', retry_safe: true }] }];
  h.context.fetch = async () => { throw new Error('must not fetch'); }; await h.app.restoreJobDraft('job', 'youtube');
  assert.match(notices[0], /尚未确认可以安全重试/);
});
test('job recovery ignores a late response after editing or clearing the composer', async () => {
  for (const action of ['edit', 'clear']) {
    const h = harness(), d = deferred(); composer(h); h.context.fetch = () => d.promise;
    const restore = h.app.restoreJobDraft('job');
    if (action === 'edit') { h.document.querySelector('#description').value = 'New content'; h.app.draftChanged(); } else h.app.clearDraft();
    d.resolve(response(material())); await restore;
    assert.equal(h.document.querySelector('#description').value, action === 'edit' ? 'New content' : '');
    assert.equal(h.app.model.assets.video, undefined); assert.equal(h.app.model.restoring, false);
    if (action === 'clear') assert.equal(h.storage.size, 0);
  }
});
test('clearing a draft resets the form and storage without deleting server assets or jobs', () => {
  const h = harness(), inputs = composer(h); inputs[0].checked = true; h.app.model.assets = material().assets; h.app.model.jobs = [{ id: 'keep' }];
  h.document.querySelector('#title').value = 'To clear'; h.app.saveDraft(); h.app.clearDraft();
  assert.equal(h.document.querySelector('#title').value, ''); assert.equal(inputs[0].checked, false); assert.equal(h.storage.size, 0); assert.equal(Object.keys(h.app.model.assets).length, 0); assert.equal(h.app.model.jobs[0].id, 'keep');
  assert.match(h.document.querySelector('#draft-status').textContent, /本地文件和任务记录仍保留/);
});
test('phase progress is readable in both history and task details without implying successful publication', () => {
  const h = harness(), job = { id: 'job', kind: 'publish', status: 'running', results: [{ platform: 'douyin', status: 'running', phase: 'uploading' }] };
  h.app.model.jobs = [job]; h.app.renderJobs(); h.app.renderJobDetails(job);
  assert.match(h.document.querySelector('#job-list').innerHTML, /向平台传输视频/); assert.match(h.document.querySelector('#dialog-content').innerHTML, /向平台传输视频/);
  assert.equal(h.app.resultProgress({ status: 'failed', phase: 'metadata' }), '最后阶段：填写标题、简介与声明');
  assert.equal(h.app.resultProgress({ status: 'submitted', phase: 'submitted' }), '进展：已提交，等待平台确认');
  assert.equal(h.app.resultProgress({ status: 'running', phase: '<script>' }), '');
  assert.match(h.app.resultProgress({status:'running',phase:'uploading',percent:42.5}),/42\.5%/);
  for (const percent of [undefined, NaN, -1, 101, '50']) assert.doesNotMatch(h.app.resultProgress({status:'running',phase:'uploading',percent}),/%/);
  assert.equal(h.app.resultProgress({status:'running',phase:'processing'}),'进展：平台正在处理视频');
  assert.match(h.app.resultProgress({status:'running',phase:'processing',phase_started_at:new Date(Date.now()-65000).toISOString()}),/已等待 1分/);
});
test('maintenance state survives partial responses, disables new publication and account actions, and renders its reason', async () => {
  const h = harness(), accountButton = element(); let calls = 0;
  h.app.model.platforms = [{ id: 'douyin' }]; h.multiples.set('[data-account-action][data-platform="douyin"]', [accountButton]);
  h.context.fetch = async () => { calls++; return response({ token: 'next', platforms: [{ id: 'douyin' }], runtime: { ready: true }, maintenance: calls === 1 ? { draining: true, active_jobs: 2, extra: 'retained' } : undefined }); };
  await h.app.refreshState(); await h.app.refreshState();
  assert.equal(h.app.model.maintenance.draining, true); assert.equal(h.app.model.maintenance.active_jobs, 2); assert.equal(h.app.model.maintenance.extra, 'retained');
  assert.equal(h.document.querySelector('#publish-button').disabled, true); assert.equal(accountButton.disabled, true);
  assert.match(h.document.querySelector('#runtime-title').textContent, /安全维护/); assert.match(h.document.querySelector('#runtime-message').textContent, /2 个已有任务/);
  await h.app.publish({ preventDefault() {} }); await h.app.accountAction('douyin', 'check'); await h.app.connectChrome('douyin'); assert.equal(calls, 2);
});
test('YouTube failure reason and limitations remain visible outside collapsed settings', () => {
  const h = harness(); h.app.model.platforms = [{ id: 'youtube', account_status: 'needs_login', account_source: 'chrome', account_message: '会话未通过 Studio 核验', note: '实验性适配', known_limitations: ['尚未验证完整发布', '重复同步未必能解决'] }];
  h.app.renderPlatforms(); h.app.updatePlatformState();
  assert.match(h.document.querySelector('[data-account-status="youtube"]').textContent, /未通过在线核验/);
  assert.equal(h.document.querySelector('[data-account-message="youtube"]').textContent, '会话未通过 Studio 核验'); assert.equal(h.document.querySelector('[data-account-message="youtube"]').hidden, false);
  const notes = h.document.querySelector('[data-platform-limitations="youtube"]'); assert.match(notes.textContent, /实验性适配.*尚未验证完整发布.*重复同步未必能解决/); assert.equal(notes.hidden, false);
  const html = h.document.querySelector('#account-list').innerHTML; assert.ok(html.indexOf('data-account-message=') < html.indexOf('<details'));
});

test('manual resolution requires explicit confirmation and only updates the local record before retry', async () => {
  const h = harness(), calls = [], confirmations = [];
  const job = { id: 'old-job', kind: 'publish', status: 'interrupted', results: [{ platform: 'youtube', status: 'needs_attention' }] };
  h.app.model.jobs = [job]; h.app.model.detailId = job.id;
  h.context.window.confirm = (question) => { confirmations.push(question); return false; };
  h.context.fetch = async (path, options) => { calls.push({ path, options }); return response({ job: { ...job, status: 'failed', results: [{ platform: 'youtube', status: 'failed', retry_safe: true }] } }); };
  await h.app.resolveResult(job.id, 'youtube', 'not_submitted'); assert.equal(calls.length, 0);
  assert.match(confirmations[0], /先核对.*创作后台.*\n\n.*只解除.*重试锁.*不会上传或发布/);
  h.context.window.confirm = () => true;
  await h.app.resolveResult(job.id, 'youtube', 'not_submitted');
  assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/jobs/old-job/resolve'); assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body), { platform: 'youtube', resolution: 'not_submitted', confirmed: true });
  assert.equal(calls[0].options.headers['X-Publisher-Token'], 'test-token');
  assert.match(h.document.querySelector('#dialog-content').innerHTML, /仅重试该平台/); assert.equal(h.requests.length, 0);
});
test('found-draft resolution is deliberate, duplicate-safe, and never creates a publish job', async () => {
  const h = harness(), d = deferred(), calls = [], questions = [];
  const job = { id: 'old-job', kind: 'publish', status: 'needs_attention', results: [{ platform: 'douyin', status: 'needs_attention' }] };
  h.app.model.jobs = [job]; h.app.model.detailId = job.id;
  h.context.window.confirm = (question) => { questions.push(question); return true; };
  h.context.fetch = (path, options) => { calls.push({ path, options }); return d.promise; };
  const first = h.app.resolveResult(job.id, 'douyin', 'submitted'); await h.app.resolveResult(job.id, 'douyin', 'submitted');
  assert.equal(calls.length, 1); assert.equal(questions.length, 1); assert.match(questions[0], /找到.*稿件.*\n\n.*只将本地任务.*不会上传/);
  assert.deepEqual(JSON.parse(calls[0].options.body), { platform: 'douyin', resolution: 'submitted', confirmed: true });
  d.resolve(response({ job: { ...job, status: 'completed', results: [{ platform: 'douyin', status: 'submitted', message: '用户已核验提交' }] } })); await first;
  const html = h.document.querySelector('#dialog-content').innerHTML; assert.match(html, /用户已核验提交/); assert.doesNotMatch(html, /仅重试该平台|data-resolve-job=/);
  assert.equal(h.requests.length, 0); assert.equal(h.app.model.resolving.size, 0);
});
test('manual result controls are shown only for uncertain platforms in a stopped publish task', async () => {
  const h = harness();
  for (const [jobStatus, resultStatus, visible] of [['interrupted', 'needs_attention', true], ['needs_attention', 'needs_attention', true], ['running', 'needs_attention', false], ['queued', 'needs_attention', false], ['failed', 'failed', false], ['completed', 'submitted', false]]) {
    const job = { id: 'job', kind: 'publish', status: jobStatus, results: [{ platform: 'youtube', status: resultStatus }] };
    h.app.model.jobs = [job]; h.app.renderJobDetails(job);
    assert.equal(h.document.querySelector('#dialog-content').innerHTML.includes('data-resolve-job='), visible, `${jobStatus} ${resultStatus}`);
    if (!visible) await h.app.resolveResult('job', 'youtube', 'not_submitted');
  }
});

test('an imported Chrome session displays a completed verification failure instead of claiming it is still checking', async () => {
  const h = harness();
  h.app.model.pairing = { platform: 'youtube', status: 'imported', code: '', jobId: 'verify' };
  h.app.model.platforms = [{ id: 'youtube', account_status: 'needs_login', account_source: 'chrome', account_message: 'Studio 拒绝导入的会话' }];
  h.app.model.jobs = [{ id: 'verify', kind: 'check', status: 'failed', results: [{ platform: 'youtube', status: 'failed' }] }];
  await h.app.pollBrowserPair();
  const text = h.document.querySelector('#browser-pair-status').textContent;
  assert.match(text, /在线核验未通过.*Studio 拒绝导入的会话.*原因与限制/); assert.doesNotMatch(text, /正在在线核验|再次.*同步|账号已验证/);
});

test('a previous verified account cannot make a newly imported session appear verified before its own check finishes', async () => {
  const h = harness();
  h.app.model.pairing = { platform: 'youtube', status: 'imported', code: '', jobId: 'new-check' };
  h.app.model.platforms = [{ id: 'youtube', account_status: 'verified', account_source: 'chrome' }];
  h.app.model.jobs = [{ id: 'old-check', status: 'completed', results: [{ platform: 'youtube', status: 'verified' }] }];
  await h.app.pollBrowserPair();
  assert.match(h.document.querySelector('#browser-pair-status').textContent, /正在在线核验.*不代表账号已验证/);
  assert.doesNotMatch(h.document.querySelector('#browser-pair-status').textContent, /会话已通过在线核验/);
});

test('manual-review state enables only advertised prepare platforms and preserves the mode across partial state responses', async () => {
  const h = harness(); let calls = 0;
  h.context.fetch = async () => { calls++; return response({ token: 'test', runtime: { ready: true }, platforms: ['douyin', 'xiaohongshu', 'youtube'].map((id) => ({ id, name: id })), ...(calls === 1 ? { submission_mode: 'manual_review', prepare_platforms: ['douyin'] } : {}) }); };
  await h.app.refreshState(); await h.app.refreshState();
  assert.equal(h.app.model.submissionMode, 'manual_review'); assert.equal(JSON.stringify(h.app.model.preparePlatforms), '["douyin"]');
  const html = h.document.querySelector('#platform-list').innerHTML;
  const checkbox = (id) => html.match(new RegExp(`<input type="checkbox"[^>]* name="platform" value="${id}"[^>]+>`))[0];
  assert.doesNotMatch(checkbox('douyin'), /disabled/); assert.doesNotMatch(html, /value="youtube"|value="xiaohongshu"/); assert.match(h.document.querySelector('#unsupported-account-list').innerHTML, /data-account-platform="youtube"/);
  assert.match(h.document.querySelector('#publish-button').innerHTML, /上传并填写/);
  assert.match(h.document.querySelector('#publish-hint').textContent, /最后由你点击发布/);
});
test('prepare payload filters unsupported stale selections and only requests manual handoff', async () => {
  const h = harness(), inputs = composer(h), calls = [], confirmations = [];
  inputs.forEach((input) => { input.checked = true; }); h.app.model.preparePlatforms = ['douyin'];
  const payload = h.app.collectPayload(); assert.equal(payload.mode, 'prepare'); assert.equal(JSON.stringify(payload.platforms), '["douyin"]');
  h.document.querySelector('#title').value = 'Manual handoff title'; h.app.model.assets = material().assets;
  h.context.window.confirm = (question) => { confirmations.push(question); return true; };
  h.context.fetch = async (path, options) => { calls.push({ path, options }); return response({ job: { id: 'prepare-job', status: 'queued', kind: 'publish' } }); };
  h.hook({ localErrors: () => [], refreshState: async () => {} }); await h.app.publish({ preventDefault() {} });
  assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/jobs'); assert.equal(JSON.parse(calls[0].options.body).mode, 'prepare');
  assert.match(confirmations[0], /上传并填写.*\n\n.*\n\n.*保留浏览器窗口.*亲自点击发布.*不会点击最终发布/);
  assert.doesNotMatch(confirmations[0], /将开始实际上传与提交/);
});
test('prepared is a terminal user-handoff state with a review link, never published or eligible for retry', () => {
  const h = harness(), job = { id: 'prepared-job', kind: 'publish', status: 'prepared', results: [{ platform: 'douyin', status: 'prepared', phase: 'prepared', retry_safe: true, url: 'https://creator.douyin.com/creator-micro/content/publish' }] };
  h.app.model.jobs = [job]; h.app.renderJobs(); h.app.renderJobDetails(job);
  assert.match(h.document.querySelector('#job-list').innerHTML, /待你发布/);
  const html = h.document.querySelector('#dialog-content').innerHTML;
  assert.match(html, /待你发布/); assert.match(html, /保留的专用浏览器窗口.*亲自点击发布/);
  assert.match(html, /href="https:\/\/creator.douyin.com\/creator-micro\/content\/publish"/); assert.match(html, /平台参考链接（不会恢复原编辑窗口）/);
  assert.doesNotMatch(html, /status-pill published|已提交 · 待平台确认|data-retry-platform=|停止此任务/);
  assert.match(html, /未发布，重新准备/); assert.match(html, /我已发布/);
  assert.equal(h.app.canRetry(job.results[0]), false);
});

test('workflow availability and descriptions follow the server allowlist as new platforms are enabled', async () => {
  const h = harness(); let enabled = ['douyin', 'xiaohongshu'];
  const platforms = [{ id: 'douyin', name: '抖音', workflow_status: 'implemented' }, { id: 'xiaohongshu', name: '小红书', workflow_status: 'unverified' }, { id: 'youtube', name: 'YouTube', workflow_status: 'manual_only', known_limitations: ['目前需在平台手动上传'] }];
  h.context.fetch = async () => response({ token: 'test', runtime: { ready: true }, submission_mode: 'manual_review', prepare_platforms: enabled, platforms });
  await h.app.refreshState();
  let html = h.document.querySelector('#platform-list').innerHTML;
  assert.doesNotMatch(html.match(/<input type="checkbox"[^>]* name="platform" value="xiaohongshu"[^>]+>/)[0], /disabled/);
  assert.doesNotMatch(html, /value="youtube"/);
  assert.match(h.document.querySelector('#workflow-platforms').textContent, /2 个可用平台/); assert.doesNotMatch(h.document.querySelector('#workflow-platforms').textContent, /仅.*抖音|YouTube/);
  assert.match(h.document.querySelector('[data-workflow-status="douyin"]').textContent, /已接入.*本人确认/);
  assert.equal(h.document.querySelector('[data-workflow-status="xiaohongshu"]').textContent, '已接入，实际平台流程仍需核验');
  assert.match(h.document.querySelector('#unsupported-account-list').innerHTML, /data-account-platform="youtube"/);
  enabled = ['douyin', 'xiaohongshu', 'youtube']; await h.app.refreshState();
  html = h.document.querySelector('#platform-list').innerHTML;
  assert.doesNotMatch(html.match(/<input type="checkbox"[^>]* name="platform" value="youtube"[^>]+>/)[0], /disabled/);
  assert.match(h.document.querySelector('#workflow-platforms').textContent, /3 个可用平台/);
});
test('session restoration removes unsupported platform selections from both composer and saved draft', async () => {
  const h = harness(), inputs = composer(h); h.app.model.preparePlatforms = ['douyin'];
  h.app.model.sessionDraft = { version: 1, fields: { title: 'Keep title', 'youtube-description': 'Keep optional text' }, platforms: ['douyin', 'youtube'], assets: {} };
  await h.app.restoreSessionDraft();
  assert.equal(inputs[0].checked, true); assert.equal(inputs[1].checked, false);
  assert.deepEqual(JSON.parse([...h.storage.values()][0]).platforms, ['douyin']);
  assert.match(h.document.querySelector('#draft-status').textContent, /youtube.*已取消勾选/);
  assert.equal(h.document.querySelector('#youtube-description').value, 'Keep optional text');
});
test('prepared restart requires checking there is no platform draft and unlocks only after confirmation without re-running', async () => {
  const h = harness(), calls = [], questions = [];
  const job = { id: 'prepared', kind: 'publish', status: 'prepared', results: [{ platform: 'douyin', status: 'prepared', phase: 'prepared' }] };
  h.app.model.jobs = [job]; h.app.model.detailId = job.id;
  h.context.window.confirm = (question) => { questions.push(question); return false; };
  h.context.fetch = async (path, options) => { calls.push({ path, options }); return response({ job: { ...job, status: 'failed', results: [{ platform: 'douyin', status: 'failed', retry_safe: true }] } }); };
  await h.app.resolveResult(job.id, 'douyin', 'not_submitted'); assert.equal(calls.length, 0);
  assert.match(questions[0], /先核对.*确认平台没有这条视频的稿件.*不再使用原来保留的窗口/);
  assert.match(questions[0], /未发布，重新准备.*只解除.*重试锁.*不会自动重跑/);
  h.context.window.confirm = () => true; await h.app.resolveResult(job.id, 'douyin', 'not_submitted');
  assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/jobs/prepared/resolve');
  assert.deepEqual(JSON.parse(calls[0].options.body), { platform: 'douyin', resolution: 'not_submitted', confirmed: true });
  assert.match(h.document.querySelector('#dialog-content').innerHTML, /仅重试该平台/); assert.equal(h.requests.length, 0);
});
test('prepared user-published confirmation only records the user result, without implying review approval', async () => {
  const h = harness(), calls = [], questions = [];
  const job = { id: 'prepared', kind: 'publish', status: 'prepared', results: [{ platform: 'douyin', status: 'prepared' }] };
  h.app.model.jobs = [job]; h.app.model.detailId = job.id;
  h.context.window.confirm = (question) => { questions.push(question); return true; };
  h.context.fetch = async (path, options) => { calls.push({ path, options }); return response({ job: { ...job, status: 'completed', results: [{ platform: 'douyin', status: 'submitted', message: '用户已核验提交' }] } }); };
  await h.app.resolveResult(job.id, 'douyin', 'submitted');
  assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/jobs/prepared/resolve');
  assert.match(questions[0], /完成这条视频的最终发布.*找到对应稿件/); assert.match(questions[0], /不代表平台已经审核通过/);
  assert.deepEqual(JSON.parse(calls[0].options.body), { platform: 'douyin', resolution: 'submitted', confirmed: true });
  assert.match(h.document.querySelector('#dialog-content').innerHTML, /用户已核验提交/); assert.equal(h.requests.length, 0);
});
test('failures and interruptions explain the last known phase and a concrete next step', () => {
  const h = harness();
  for (const result of [{ platform: 'douyin', status: 'failed', phase: 'cover', retry_safe: true }, { platform: 'douyin', status: 'needs_attention', phase: 'metadata' }, { platform: 'douyin', status: 'interrupted' }]) {
    h.app.renderJobDetails({ id: 'problem', kind: 'publish', status: result.status, results: [result] });
    const html = h.document.querySelector('#dialog-content').innerHTML;
    assert.match(html, /最后阶段：/); assert.match(html, /下一步：/);
    if (result.status === 'failed') assert.match(html, /设置封面.*恢复材料.*检查失败原因/);
    else assert.match(html, /平台创作后台检查稿件/);
  }
});
test('the page presents upload-fill, retained-window, and user-publication as three explicit steps', () => {
  const html = fs.readFileSync(require('node:path').join(__dirname, '../static/index.html'), 'utf8');
  assert.match(html, /<ol class="manual-workflow"[^>]*>.*上传并填写.*保留窗口.*本人发布.*<\/ol>/);
  assert.ok(html.includes('id="workflow-platforms"')); assert.doesNotMatch(html, /当前仅支持抖音/);
});

function coverAttentionJob(extra = {}, resultExtra = {}) {
  return { id: 'cover-job', kind: 'publish', mode: 'prepare', status: 'needs_attention', ...extra, results: [{ platform: 'xiaohongshu', status: 'needs_attention', phase: 'cover', ...resultExtra }] };
}
test('resume-cover control is restricted to a stopped prepare task with Xiaohongshu attention at the cover phase', async () => {
  const h = harness(); let requests = 0, confirmations = 0;
  h.context.fetch = async () => { requests++; throw new Error('must not fetch'); }; h.context.window.confirm = () => { confirmations++; return true; };
  const cases = [
    [coverAttentionJob(), true], [coverAttentionJob({ status: 'interrupted' }), true],
    [coverAttentionJob({ mode: undefined }), false], [coverAttentionJob({ mode: 'publish' }), false],
    [coverAttentionJob({ status: 'running' }), false], [coverAttentionJob({ status: 'queued' }), false],
    [coverAttentionJob({}, { platform: 'douyin' }), false], [coverAttentionJob({}, { platform: 'youtube' }), false],
    [coverAttentionJob({}, { status: 'failed' }), false], [coverAttentionJob({}, { status: 'prepared' }), false],
    [coverAttentionJob({}, { phase: 'uploading' }), false], [coverAttentionJob({}, { phase: undefined }), false]
  ];
  for (const [job, visible] of cases) {
    h.app.model.jobs = [job]; h.app.renderJobDetails(job);
    assert.equal(h.document.querySelector('#dialog-content').innerHTML.includes('data-resume-cover-job='), visible, JSON.stringify(job));
    if (!visible) await h.app.resumeCover(job.id);
  }
  assert.equal(requests, 0); assert.equal(confirmations, 0);
});
test('resume-cover cancellation sends no request and confirmation explains original-page reuse without reupload', async () => {
  const h = harness(), questions = []; h.app.model.jobs = [coverAttentionJob()];
  h.context.window.confirm = (question) => { questions.push(question); return false; };
  h.context.fetch = async () => { throw new Error('must not fetch'); }; await h.app.resumeCover('cover-job');
  assert.equal(questions.length, 1); assert.match(questions[0], /复用保留的原编辑页.*原视频、标题和正文必须仍在/);
  assert.match(questions[0], /只接续同一任务的封面步骤.*不重传视频.*不运行其他平台.*不会点击最终发布/);
  assert.equal(h.app.model.coverResuming.size, 0);
});
test('resume-cover is duplicate-safe, resumes the same job, and never resolves or creates another task', async () => {
  const h = harness(), d = deferred(), calls = []; let confirmations = 0, refreshes = 0;
  const job = coverAttentionJob(); job.results.push({ platform: 'douyin', status: 'prepared', phase: 'prepared' });
  h.app.model.jobs = [job]; h.app.model.detailId = job.id;
  h.context.window.confirm = () => { confirmations++; return true; };
  h.context.fetch = (path, options) => { calls.push({ path, options }); return d.promise; };
  h.hook({ refreshState: async () => { refreshes++; } });
  const first = h.app.resumeCover(job.id); await h.app.resumeCover(job.id); await h.app.resolveResult(job.id, 'xiaohongshu', 'not_submitted');
  assert.equal(calls.length, 1); assert.equal(confirmations, 1); assert.equal(calls[0].path, '/api/jobs/cover-job/resume-cover');
  assert.equal(calls[0].options.method, 'POST'); assert.equal(calls[0].options.headers['X-Publisher-Token'], 'test-token');
  assert.deepEqual(JSON.parse(calls[0].options.body), { platform: 'xiaohongshu' });
  assert.match(h.document.querySelector('#dialog-content').innerHTML, /data-resume-cover-job="cover-job" disabled>正在接续封面/);
  const resumed = { ...job, status: 'queued', results: [{ platform: 'xiaohongshu', status: 'queued', phase: 'cover' }, job.results[1]] };
  d.resolve(response({ job: resumed })); await first;
  assert.equal(refreshes, 1); assert.equal(h.app.model.jobs.length, 1); assert.equal(h.app.model.jobs[0].id, job.id); assert.equal(h.app.model.jobs[0].status, 'queued');
  assert.equal(h.app.model.jobs[0].results[1].status, 'prepared'); assert.equal(h.app.model.coverResuming.size, 0); assert.equal(h.requests.length, 0);
  assert.match(h.document.querySelector('#dialog-content').innerHTML, /等待执行/); assert.doesNotMatch(h.document.querySelector('#dialog-content').innerHTML, /data-resume-cover-job=/);
});
test('resume-cover stays blocked during maintenance or concurrent result resolution', async () => {
  const h = harness(); h.app.model.jobs = [coverAttentionJob()]; let confirmations = 0;
  h.context.window.confirm = () => { confirmations++; return true; }; h.context.fetch = async () => { throw new Error('must not fetch'); };
  h.app.model.maintenance.draining = true; await h.app.resumeCover('cover-job');
  h.app.renderJobDetails(h.app.model.jobs[0]); assert.match(h.document.querySelector('#dialog-content').innerHTML, /data-resume-cover-job="cover-job" disabled/);
  h.app.model.maintenance.draining = false; h.app.model.resolving.add('cover-job:xiaohongshu'); await h.app.resumeCover('cover-job');
  assert.equal(confirmations, 0);
});
test('resume-cover request failure is reported without inventing success or falling back to a new job', async () => {
  const h = harness(), calls = [], notices = []; h.app.model.jobs = [coverAttentionJob()]; h.app.model.detailId = 'cover-job';
  h.hook({ toast: (message) => notices.push(message) });
  h.context.fetch = async (path, options) => { calls.push({ path, options }); return { ok: false, status: 409, json: async () => ({ error: '原编辑页已关闭' }) }; };
  await h.app.resumeCover('cover-job');
  assert.equal(calls.length, 1); assert.equal(calls[0].path, '/api/jobs/cover-job/resume-cover');
  assert.equal(h.app.model.jobs[0].status, 'needs_attention'); assert.equal(h.app.model.coverResuming.size, 0);
  assert.match(notices[0], /续接未确认.*刷新任务状态.*原编辑页已关闭/); assert.equal(h.requests.length, 0);
});

test('fixture-only platform cards disclose source-only support and remain unselectable', async () => {
  const h = harness();
  h.context.fetch = async () => response({ token: 'test', runtime: { ready: true }, submission_mode: 'manual_review', prepare_platforms: ['douyin'], platforms: [{ id: 'bilibili', name: 'B站', workflow_status: 'fixture_only', prepare_supported: false, known_limitations: ['仅合成适配，未通过真实账号验收'] }] });
  await h.app.refreshState();
  assert.equal(h.document.querySelector('[data-workflow-status="bilibili"]').textContent, '上传准备暂未开放');
  assert.doesNotMatch(h.document.querySelector('#platform-list').innerHTML, /value="bilibili"/); assert.match(h.document.querySelector('#unsupported-account-list').innerHTML, /data-account-platform="bilibili"/);
});

const localAsset = (id, kind = 'video') => ({ id, kind, name: kind === 'video' ? `${id}.mp4` : `${id}.png`, size: 1024, width: 1920, height: 1080, duration: kind === 'video' ? '125.8' : null, url: `/api/assets/${id}` });
const templateKey = 'video-publisher.copy-templates.v1';
function explicitDeclarations(h) {
  for (const [id, value] of Object.entries({ 'ai-generated': 'true', 'youtube-kids': 'false', 'youtube-altered': 'true', 'youtube-visibility': 'unlisted', 'weibo-content-type': 'original' })) h.document.querySelector(`#${id}`).value = value;
}
function assertDeclarationsCleared(h) {
  for (const id of ['ai-generated', 'youtube-kids', 'youtube-altered', 'youtube-visibility', 'weibo-content-type']) assert.equal(h.document.querySelector(`#${id}`).value, '', id);
}
test('explicit copy template save persists only whitelisted copy and platform choices', () => {
  const h = harness(), inputs = composer(h); inputs[0].checked = true;
  h.app.model.token = 'TOKEN-SECRET'; h.app.model.pairing = { code: 'PAIR-SECRET' }; h.app.model.assets.video = { id: 'ASSET-SECRET', bytes: 'FILE-BYTES' };
  h.document.querySelector('#template-name').value = '每周科普'; h.document.querySelector('#title').value = '  A title  '; h.document.querySelector('#description').value = '\nKeep body\n'; h.document.querySelector('#youtube-description').value = 'Platform copy'; explicitDeclarations(h);
  h.app.saveTemplate();
  const raw = h.localStorage.get(templateKey), saved = JSON.parse(raw)[0];
  assert.equal(saved.name, '每周科普'); assert.equal(saved.fields.title, '  A title  '); assert.equal(saved.fields.description, '\nKeep body\n'); assert.equal(saved.fields['youtube-description'], 'Platform copy'); assert.deepEqual(saved.platforms, ['douyin']);
  for (const forbidden of ['TOKEN-SECRET', 'PAIR-SECRET', 'ASSET-SECRET', 'FILE-BYTES', 'ai-generated', 'youtube-kids', 'youtube-altered', 'youtube-visibility', 'weibo-content-type']) assert.ok(!raw.includes(forbidden), forbidden);
  assert.equal(h.requests.length, 0); assert.match(h.document.querySelector('#template-status').textContent, /关闭页面后仍可使用/);
  const reloaded = h.app.readTemplates(); assert.equal(reloaded.length, 1); assert.equal(reloaded[0].fields.title, '  A title  ');
});
test('template reading rejects malformed entries whole, explains it and never partially truncates copy', () => {
  const h = harness(); composer(h); h.document.querySelector('#title').value = 'Current';
  for (const raw of ['{broken', '{}', 'x'.repeat(524289)]) { h.localStorage.set(templateKey, raw); assert.equal(h.app.readTemplates().length, 0); }
  h.localStorage.set(templateKey, JSON.stringify([{ id: 'saved', name: '<img src=x>', fields: { title: 'Saved', token: 'SECRET', 'unknown-description': 'NO', 'ai-generated': 'true', description: 'x'.repeat(50001) }, platforms: ['douyin', '<script>'] }]));
  assert.equal(h.app.readTemplates().length, 0); assert.match(h.document.querySelector('#template-status').textContent, /未加载.*没有改写原数据/);
  h.localStorage.set(templateKey, JSON.stringify([{ id: 'saved', name: '<img src=x>', fields: { title: '  Saved  ' }, platforms: ['douyin'] }])); const read = h.app.readTemplates(); assert.equal(read[0].fields.title, '  Saved  ');
  assert.equal(h.document.querySelector('#title').value, 'Current'); h.app.model.templates = read; h.app.renderTemplates(); assert.match(h.document.querySelector('#template-select').innerHTML, /&lt;img/); assert.doesNotMatch(h.document.querySelector('#template-select').innerHTML, /<img/);
});
test('template apply is explicit, preserves materials, resets declarations and filters unavailable platforms', () => {
  const h = harness(), inputs = composer(h); inputs[0].checked = true; h.app.model.preparePlatforms = ['douyin'];
  h.app.model.assets = { video: localAsset('kept'), cover: localAsset('cover', 'cover') }; explicitDeclarations(h); h.document.querySelector('#title').value = 'Previous';
  h.app.model.templates = [{ id: 'saved', name: 'Reusable', fields: { title: 'New', description: '\nNew body\n', 'youtube-description': 'YT text' }, platforms: ['douyin', 'youtube'] }]; h.document.querySelector('#template-select').value = 'saved';
  h.context.window.confirm = () => false; h.app.applyTemplate(); assert.equal(h.document.querySelector('#title').value, 'Previous'); assert.equal(h.document.querySelector('#ai-generated').value, 'true');
  const confirmations = []; h.context.window.confirm = (text) => { confirmations.push(text); return true; }; h.app.applyTemplate();
  assert.match(confirmations[0], /保留已选视频与封面.*声明.*可见范围将清空/); assert.equal(h.document.querySelector('#title').value, 'New'); assert.equal(h.document.querySelector('#description').value, '\nNew body\n'); assert.equal(h.app.model.assets.video.id, 'kept'); assert.equal(h.app.model.assets.cover.id, 'cover'); assert.equal(inputs[0].checked, true); assert.equal(inputs[1].checked, false); assertDeclarationsCleared(h);
  assert.equal(h.document.querySelector('#validation-panel').hidden, true); assert.equal(h.requests.length, 0); assert.match(h.document.querySelector('#template-status').textContent, /没有发起上传/); assert.equal(h.document.querySelector('#publish-button').disabled, true);
});
test('template overwrite and deletion require deliberate confirmation; cancelled or failed save preserves originals', () => {
  const h = harness(); composer(h); const original = { id: 'saved', name: 'Same', fields: { title: 'Original' }, platforms: [] }; h.app.model.templates = [original]; h.localStorage.set(templateKey, JSON.stringify([original])); h.document.querySelector('#template-name').value = 'Same'; h.document.querySelector('#template-select').value = 'saved'; h.document.querySelector('#title').value = 'New';
  h.context.window.confirm = () => false; h.app.saveTemplate(); h.app.deleteTemplate(); assert.equal(h.app.model.templates[0].fields.title, 'Original');
  h.context.window.confirm = () => true; h.context.window.localStorage.setItem = () => { throw new Error('storage full'); }; h.app.saveTemplate(); assert.equal(h.app.model.templates[0].fields.title, 'Original'); assert.match(h.document.querySelector('#template-status').textContent, /未能保存/);
  h.context.window.localStorage.setItem = (key, value) => h.localStorage.set(key, value); h.app.deleteTemplate(); assert.equal(h.app.model.templates.length, 0); assert.equal(h.document.querySelector('#title').value, 'New'); assert.deepEqual(JSON.parse(h.localStorage.get(templateKey)), []);
});
test('template count and per-field length bounds do not silently truncate user copy', () => {
  const h = harness(); composer(h); const notices = []; h.hook({ toast: (text) => notices.push(text) }); h.document.querySelector('#template-name').value = 'New';
  h.document.querySelector('#title').value = 'x'.repeat(50001); h.app.saveTemplate(); assert.equal(h.localStorage.size, 0); assert.match(notices.at(-1), /50,000/);
  h.document.querySelector('#title').value = 'Small'; h.app.model.templates = Array.from({ length: 8 }, (_, i) => ({ id: `copy-${i}`, name: `Existing ${i}`, fields: { title: 'Old' }, platforms: [] })); h.app.saveTemplate(); assert.equal(h.localStorage.size, 0); assert.match(notices.at(-1), /8 份/);
});
test('recent materials list and selection use only GET and revalidate file before binding', async () => {
  const h = harness(), calls = []; composer(h); const asset = localAsset('recent');
  h.context.fetch = async (path, options) => { calls.push([path, options.method]); return response(path.includes('?') ? { assets: [asset], has_more: true } : { asset }); };
  await h.app.openRecentAssets('video'); assert.match(h.document.querySelector('#asset-picker-status').textContent, /最近 1 个.*更早素材/); assert.equal(h.app.model.assets.video, undefined);
  await h.app.useRecentAsset('recent'); assert.equal(h.app.model.assets.video.id, 'recent'); assert.deepEqual(calls, [['/api/assets?kind=video&limit=12', 'GET'], ['/api/assets/recent/info', 'GET']]); assert.equal(h.requests.length, 0); assert.equal(h.document.querySelector('#asset-picker').open, false);
});
test('recent cover picker rejects nonlocal or wrong-kind entries and escapes file names', async () => {
  const h = harness(); composer(h); const asset = { ...localAsset('safe', 'cover'), name: '<img src=x onerror=alert(1)>' };
  h.context.fetch = async () => response({ assets: [asset, localAsset('wrong'), { ...localAsset('remote', 'cover'), url: 'https://evil.test/image.png' }, { ...localAsset('../bad', 'cover') }] });
  await h.app.openRecentAssets('landscape'); assert.equal(h.app.model.assetPicker.assets.length, 1); const html = h.document.querySelector('#asset-picker-list').innerHTML; assert.match(html, /&lt;img/); assert.doesNotMatch(html, /<img src=x|evil.test|data-use-asset="wrong"/);
});
test('closing or switching a material picker discards late list responses', async () => {
  const h = harness(), first = deferred(); composer(h); let calls = 0;
  h.context.fetch = async () => { calls++; return calls === 1 ? first.promise : response({ assets: [localAsset('cover', 'cover')] }); };
  const old = h.app.openRecentAssets('video'); await h.app.openRecentAssets('cover'); first.resolve(response({ assets: [localAsset('late')] })); await old;
  assert.equal(h.app.model.assetPicker.slot, 'cover'); assert.equal(h.app.model.assetPicker.assets[0].id, 'cover');
  const second = deferred(); h.context.fetch = () => second.promise; const pending = h.app.openRecentAssets('video'); h.app.closeAssetPicker(); second.resolve(response({ assets: [localAsset('later')] })); await pending; assert.equal(h.app.model.assetPicker, null); assert.equal(h.document.querySelector('#asset-picker').open, false);
});
test('recent selection is duplicate-safe and ignores late results after close or composer edit', async () => {
  for (const action of ['close', 'edit']) {
    const h = harness(), data = deferred(); composer(h); const asset = localAsset('recent'); let calls = 0;
    h.context.fetch = async (path) => { calls++; return path.includes('?') ? response({ assets: [asset] }) : data.promise; }; await h.app.openRecentAssets('video');
    const selection = h.app.useRecentAsset('recent'); await h.app.useRecentAsset('recent'); assert.equal(calls, 2);
    if (action === 'close') h.app.closeAssetPicker(); else { h.document.querySelector('#title').value = 'New typing'; h.app.draftChanged(); }
    data.resolve(response({ asset })); await selection; assert.equal(h.app.model.assets.video, undefined); if (action === 'edit') assert.match(h.document.querySelector('#asset-picker-status').textContent, /材料已修改/);
  }
});
test('deleted or mismatched recent assets keep current selected material and explain failure', async () => {
  for (const status of [404, 200]) {
    const h = harness(); composer(h); h.app.model.assets.cover = localAsset('kept', 'cover');
    h.context.fetch = async (path) => path.includes('?') ? response({ assets: [localAsset('gone', 'cover')] }) : status === 404 ? { ok: false, status: 404, json: async () => ({ error: '文件已不存在' }) } : response({ asset: localAsset('gone', 'video') });
    await h.app.openRecentAssets('cover'); await h.app.useRecentAsset('gone'); assert.equal(h.app.model.assets.cover.id, 'kept'); assert.match(h.document.querySelector('#asset-picker-status').textContent, /未能选用.*当前文件保持不变/); if (status === 404) assert.equal(h.app.model.assetPicker.assets.length, 0);
  }
});
test('changing or removing video clears prior declarations; reusing the same video leaves deliberate choices', async () => {
  const h = harness(); composer(h); h.app.model.assets.video = localAsset('old'); explicitDeclarations(h);
  h.context.fetch = async (path) => path.includes('?') ? response({ assets: [localAsset('old'), localAsset('new')] }) : response({ asset: localAsset(path.includes('/new/') ? 'new' : 'old') });
  await h.app.openRecentAssets('video'); await h.app.useRecentAsset('old'); assert.equal(h.document.querySelector('#ai-generated').value, 'true');
  await h.app.openRecentAssets('video'); await h.app.useRecentAsset('new'); assertDeclarationsCleared(h);
  explicitDeclarations(h); h.app.removeAsset('video'); assertDeclarationsCleared(h); assert.equal(h.app.model.assets.video, undefined);
  await h.app.openRecentAssets('video'); await h.app.useRecentAsset('old'); assertDeclarationsCleared(h);
});
test('successful new video upload clears declarations and unsupported file containers never start upload', async () => {
  const h = harness(); composer(h); h.app.model.assets.video = localAsset('old'); explicitDeclarations(h);
  for (const name of ['file.avi', 'file.mkv']) await h.app.uploadFile('video', { name, type: 'video/x-matroska' }); assert.equal(h.requests.length, 0);
  const upload = h.app.uploadFile('video', { name: 'new.mp4', type: 'video/mp4' }); h.requests[0].succeed(localAsset('new')); await upload; assertDeclarationsCleared(h);
});
test('video preview stays local, displays probe dimensions and duration, and pauses/clears on removal', () => {
  const h = harness(), z = h.zone('video'), video = z.children.get('video'); let pauses = 0; video.pause = () => pauses++;
  h.app.model.assets.video = localAsset('preview'); h.app.renderAsset('video'); assert.equal(video.src, 'http://127.0.0.1:8765/api/assets/preview'); assert.equal(video.hidden, false); assert.match(z.children.get('.asset-meta').textContent, /1920 × 1080.*2:05/); const before = pauses; h.app.renderAsset('video'); assert.equal(pauses, before);
  h.app.videoPreviewUnavailable(); assert.equal(h.app.model.assets.video.id, 'preview'); assert.match(z.children.get('.video-preview-notice').textContent, /无法预览.*已保留/); assert.equal(z.children.get('.video-preview-notice').hidden, false);
  h.app.removeAsset('video'); assert.equal(video.src, undefined); assert.equal(video.hidden, true); assert.equal(z.children.get('.video-preview-notice').hidden, true);
  h.app.model.assets.video = { ...localAsset('remote'), url: 'https://evil.test/video.mp4' }; h.app.renderAsset('video'); assert.equal(video.src, undefined); assert.equal(video.hidden, true);
});
test('live readiness blocks incomplete forms and reveals platform-specific fields without choosing declarations', () => {
  const h = harness(), inputs = composer(h); inputs[1].checked = true; h.app.updateActions(); assert.equal(h.document.querySelector('#publish-button').disabled, true); assert.match(h.document.querySelector('#readiness-items').innerHTML, /data-focus-field="youtube-visibility"/); assert.match(h.document.querySelector('[data-platform-next="youtube"]').innerHTML, /下一步/); assert.equal(h.document.querySelector('#youtube-visibility').value, '');
  h.app.model.assets = { video: localAsset('video'), cover: localAsset('cover', 'cover') }; h.document.querySelector('#title').value = 'Ready'; h.document.querySelector('#ai-generated').value = 'false'; h.document.querySelector('#youtube-visibility').value = 'private'; h.document.querySelector('#youtube-kids').value = 'false'; h.document.querySelector('#youtube-altered').value = 'false'; h.app.updateActions();
  assert.equal(h.document.querySelector('#publish-button').disabled, false); assert.match(h.document.querySelector('#readiness-summary').textContent, /必填项已齐.*检查发布材料/); assert.equal(h.requests.length, 0);
});
test('live readiness counts Unicode titles and combined body plus tags, without truncating any copy', () => {
  const h = harness(); const payload = { title: '😀'.repeat(21), description: 'x'.repeat(999), tags: ['tag'], platforms: ['xiaohongshu'], ai_generated: true, video_id: 'v', cover_id: 'c' };
  const issues = h.app.editorIssues(payload); assert.ok(issues.some((issue) => issue.target === 'xiaohongshu-title')); assert.ok(issues.some((issue) => issue.target === 'xiaohongshu-description')); assert.ok(issues.some((issue) => issue.target === 'platform-xiaohongshu')); assert.equal(Array.from(payload.title).length, 21);
});
test('jump-to-field opens collapsed settings and focuses the actual input without transmitting anything', () => {
  const h = harness(), details = element({ open: false }), card = element(); const input = element({ closest: (selector) => selector === 'details' ? details : selector === '.platform-card' ? card : null }); h.singles.set('#youtube-kids', input);
  h.app.focusField('youtube-kids'); assert.equal(details.open, true); assert.equal(input.focused, true); assert.equal(h.requests.length, 0);
});
test('restoring a mixed task selects only explicitly safe-to-retry platforms and preserves other results', async () => {
  const h = harness(), inputs = composer(h, ['douyin', 'youtube', 'xiaohongshu']); h.app.model.preparePlatforms = ['douyin', 'youtube', 'xiaohongshu'];
  h.app.model.jobs = [{ id: 'mixed', kind: 'publish', status: 'partial', results: [{ platform: 'douyin', status: 'prepared' }, { platform: 'youtube', status: 'submitted' }, { platform: 'xiaohongshu', status: 'failed', retry_safe: true }] }];
  const data = material(); data.payload.platforms = ['douyin', 'youtube', 'xiaohongshu']; h.context.fetch = async () => response(data); await h.app.restoreJobDraft('mixed');
  assert.deepEqual(inputs.map((input) => input.checked), [false, false, true]); assert.match(h.document.querySelector('#recovery-notice').textContent, /尚未确认可安全重试，已取消勾选.*专用浏览器窗口/); assert.equal(h.app.model.jobs[0].results[0].status, 'prepared'); assert.equal(h.requests.length, 0);
});
test('platform retry recovery aborts if the platform result changes while the draft is loading', async () => {
  const h = harness(), d = deferred(), notices = []; composer(h); h.hook({ toast: (text) => notices.push(text) }); const job = { id: 'job', kind: 'publish', status: 'failed', results: [{ platform: 'youtube', status: 'failed', retry_safe: true }] }; h.app.model.jobs = [job];
  h.context.fetch = () => d.promise; const restore = h.app.restoreJobDraft('job', 'youtube'); job.results[0] = { platform: 'youtube', status: 'prepared' }; d.resolve(response(material())); await restore;
  assert.equal(h.app.model.assets.video, undefined); assert.match(notices.at(-1), /状态已变化/);
});

test('missing saved video clears old declarations before a replacement can be chosen', async () => {
  for (const mode of ['session', 'job']) {
    const h = harness(); composer(h); explicitDeclarations(h);
    if (mode === 'session') {
      h.app.model.sessionDraft = { version: 1, fields: { title: 'Old draft', 'ai-generated': 'true', 'youtube-kids': 'false', 'youtube-altered': 'true', 'youtube-visibility': 'public' }, platforms: ['youtube'], assets: { video: 'missing' } };
      h.context.fetch = async () => ({ ok: false, status: 404, json: async () => ({ error: 'missing' }) }); await h.app.restoreSessionDraft();
    } else {
      const data = material(); data.assets.video = null; h.app.model.jobs = [{ id: 'job', kind: 'publish', results: [{ platform: 'youtube', status: 'failed', retry_safe: true }] }]; h.context.fetch = async () => response(data); await h.app.restoreJobDraft('job');
    }
    assert.equal(h.app.model.assets.video, undefined); assertDeclarationsCleared(h); assert.equal(h.document.querySelector('#publish-button').disabled, true);
  }
});
test('oversized template collections and long names are rejected without changing stored text', () => {
  const h = harness(), notices = []; composer(h); h.hook({ toast: (text) => notices.push(text) }); h.document.querySelector('#title').value = 'Keep all'; h.document.querySelector('#template-name').value = 'n'.repeat(41); h.app.saveTemplate(); assert.equal(h.localStorage.size, 0); assert.match(notices.at(-1), /40/);
  const fields = Object.fromEntries(['title', 'description', 'tags', 'douyin-title', 'douyin-description', 'youtube-title'].map((key) => [key, 'x'.repeat(50000)])); h.app.model.templates = [{ id: 'old', name: 'Large', fields, platforms: [] }]; h.localStorage.set(templateKey, JSON.stringify(h.app.model.templates)); const before = h.localStorage.get(templateKey);
  for (const [key, value] of Object.entries(fields)) h.document.querySelector(`#${key}`).value = value; h.document.querySelector('#template-name').value = 'Another'; h.app.saveTemplate(); assert.equal(h.localStorage.get(templateKey), before); assert.equal(h.app.model.templates.length, 1); assert.match(h.document.querySelector('#template-status').textContent, /总量过大.*没有保存/);
});


test('view navigation preserves the composer and isolates account controls', () => {
  const h = harness();
  const views = ['compose','accounts','history'].map(view => element({dataset:{view}}));
  const links = views.map(view => element({href:'#'+view.dataset.view}));
  h.multiples.set('[data-view]',views);h.multiples.set('.nav-item',links);
  h.document.querySelector('#title').value = '保持这份文案';
  h.app.showView('accounts');
  assert.equal(views[0].hidden,true);assert.equal(views[1].hidden,false);
  assert.equal(links[1]['aria-current'],'page');assert.equal(h.context.window.location.hash,'accounts');
  h.app.showView('compose');assert.equal(h.document.querySelector('#title').value,'保持这份文案');
  h.app.showView('unknown');assert.equal(h.app.model.view,'compose');
});
test('temporary account check failure offers retry and retains saved-session wording', () => {
  const h=harness();h.app.model.platforms=[{id:'xiaohongshu',account_status:'verification_unavailable',session_saved:true,account_message:'网络暂时无法连接'}];
  h.app.updatePlatformState();
  const primary=h.document.querySelector('[data-account-primary="xiaohongshu"]');
  assert.equal(primary.dataset.accountAction,'check');assert.equal(primary.textContent,'重试检查');
  assert.match(h.document.querySelector('[data-account-context="xiaohongshu"]').textContent,/登录仍保留/);
  assert.equal(h.document.querySelector('[data-account-message="xiaohongshu"]').hidden,false);
});
test('history keeps account checks out of the default publishing list', () => {
  const h=harness();h.app.model.jobs=[{id:'p',kind:'publish',status:'prepared',payload:{title:'保留的视频'},results:[]},{id:'a',kind:'check',status:'completed',results:[{platform:'bilibili',status:'verified'}]}];
  h.app.renderJobs();assert.match(h.document.querySelector('#job-list').innerHTML,/data-job="p"/);assert.doesNotMatch(h.document.querySelector('#job-list').innerHTML,/data-job="a"/);
  h.app.model.historyFilter='accounts';h.app.renderJobs();assert.match(h.document.querySelector('#job-list').innerHTML,/data-job="a"/);assert.doesNotMatch(h.document.querySelector('#job-list').innerHTML,/data-job="p"/);
});
