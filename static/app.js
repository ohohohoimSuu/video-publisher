"use strict";

(() => {
  // Hosted files contain no account data. Only a fixed loopback backend is
  // accepted; URL query parameters cannot redirect credentials or uploads.
  const hosted = window.PUBLISHER_LOCAL_BACKEND === "http://127.0.0.1:8765";
  const apiOrigin = hosted ? window.PUBLISHER_LOCAL_BACKEND : window.location.origin;
  const apiURL = (path) => hosted ? new URL(path, apiOrigin).href : path;
  const platformBranding = [
    { id: "douyin", name: "抖音", icon: "♪" },
    { id: "xiaohongshu", name: "小红书", icon: "小红书" },
    { id: "bilibili", name: "哔哩哔哩", icon: "bilibili" },
    { id: "youtube", name: "YouTube", icon: "▶" },
    { id: "tencent", name: "微信视频号", icon: "视" }, { id: "kuaishou", name: "快手", icon: "快" },
    { id: "baijiahao", name: "百家号", icon: "百" }, { id: "alipay", name: "支付宝生活号", icon: "支" },
    { id: "weibo", name: "微博", icon: "微" }, { id: "hupu", name: "虎扑", icon: "虎" }, { id: "tiktok", name: "TikTok", icon: "♪" },
  ];
  const runningStatuses = new Set(["queued", "pending", "running", "in_progress", "waiting", "starting"]);
  const model = { view: "compose", historyFilter: "publish", token: null, platforms: [], jobs: [], runtime: null, submissionMode: "manual_review", preparePlatforms: [], maintenance: { draining: false, active_jobs: 0 }, browserBridge: null, nativeChrome: null, pairing: null, assets: {}, uploading: new Set(), submitting: false, validating: false, accountBusy: new Set(), cancelRequested: new Set(), resolving: new Set(), coverResuming: new Set(), refreshing: false, detailId: null, toastTimer: null, draftRevision: 0, restoreRequest: 0, restoring: false, sessionRestoreDone: false, sessionDraft: null, assetPicker: null, templates: [], templateSerial: 0 };
  const draftStorageKey = "video-publisher.draft.v1";
  const templateStorageKey = "video-publisher.copy-templates.v1";
  const assetSlots = ["video", "cover", "landscape"];
  const assetLabels = { video: "视频", cover: "主封面", landscape: "横版封面" };
  const validId = (value) => typeof value === "string" && /^[a-zA-Z0-9_-]+$/.test(value);
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const platformName = (id) => model.platforms.find((p) => p.id === id)?.name || platformBranding.find((p) => p.id === id)?.name || id || "平台";
  const isDisabled = (platform) => Boolean(platform.disabled || platform.capabilities?.disabled || platform.available === false);
  const supportsPrepare = (id) => model.preparePlatforms.includes(id);
  const supportsChrome = (id) => model.browserBridge?.state === "available" && ["douyin", "xiaohongshu", "youtube"].includes(id) && model.browserBridge.platforms?.includes(id);
  const safeLink = (url) => { if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return null; try { const parsed = new URL(url, window.location.origin); return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null; } catch { return null; } };
  const assetLink = (url) => { if (typeof url !== "string") return null; try { const parsed = new URL(url, apiOrigin); return parsed.origin === apiOrigin && /^\/api\/assets\/[a-zA-Z0-9_-]+$/.test(parsed.pathname) && !parsed.search && !parsed.hash ? parsed.href : null; } catch { return null; } };
  const errorMessage = (error) => error?.message || "操作失败，请查看本地服务状态后重试。";
  const formatSize = (size) => { if (!Number.isFinite(Number(size))) return ""; const bytes = Number(size); return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`; };
  const formatDate = (date) => { if (!date) return "时间未知"; const parsed = new Date(typeof date === "number" && date < 1e12 ? date * 1000 : date); return Number.isNaN(parsed.getTime()) ? String(date) : parsed.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); };
  const statusInfo = (status) => ({ queued: ["等待执行", "running"], pending: ["等待执行", "running"], starting: ["正在启动", "running"], running: ["执行中", "running"], in_progress: ["执行中", "running"], waiting: ["等待操作", "running"], prepared: ["待你发布", "prepared"], submitted: ["已提交 · 待平台确认", "submitted"], published: ["已发布", "published"], verified: ["已验证", "verified"], verification_unavailable: ["暂时无法检查", "verification_unavailable"], success: ["执行完成", "completed"], completed: ["执行完成", "completed"], succeeded: ["执行完成", "completed"], done: ["执行结束", "completed"], failed: ["失败", "failed"], error: ["检查失败", "error"], partial: ["部分完成", "partial"], partial_success: ["部分完成", "partial"], cancelled: ["已取消", "cancelled"], skipped: ["已跳过", "skipped"], needs_attention: ["结果待核验", "needs_attention"], interrupted: ["执行中断 · 待核验", "interrupted"], needs_login: ["需要登录", "submitted"], login_required: ["需要登录", "submitted"] }[status] || [status || "状态待确认", "unknown"]);
  const pill = (status) => { const [label, kind] = statusInfo(status); return `<span class="status-pill ${escape(kind)}">${escape(label)}</span>`; };

  function toast(message, isError = false) {
    const element = $("#toast");
    clearTimeout(model.toastTimer);
    element.textContent = message;
    element.classList.toggle("error", isError);
    element.hidden = false;
    model.toastTimer = setTimeout(() => { element.hidden = true; }, isError ? 6500 : 4500);
  }

  async function api(path, { method = "GET", body } = {}) {
    const headers = {};
    if (method !== "GET") {
      if (!model.token) throw new Error("尚未连接本地服务，请先点击「重新检查」。");
      headers["X-Publisher-Token"] = model.token;
      headers["Content-Type"] = "application/json";
    }
    let response;
    try {
      response = await fetch(apiURL(path), { method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", ...(hosted ? { mode: "cors", credentials: "omit", targetAddressSpace: "loopback" } : {}) });
    } catch (error) {
      if (hosted) throw new Error("无法连接这台电脑的发布台。请先运行「启动发布台」，并允许浏览器访问本机服务，再点「重新检查」。");
      throw error;
    }
    let data;
    try { data = await response.json(); } catch { throw new Error(`本地服务返回了无法读取的响应（HTTP ${response.status}）。`); }
    if (!response.ok) { const error = new Error(typeof data.error === "string" ? data.error : `请求失败（HTTP ${response.status}）`); error.errors = data.errors; error.httpStatus = response.status; throw error; }
    return data;
  }

  // Only editable form values and opaque local asset IDs belong in browser storage.
  // Tokens, pairing codes, account data and file bytes never enter this draft.
  function draftFields() {
    return ["title", "description", "tags", "ai-generated", "bilibili-category", "bilibili-declaration", "youtube-visibility", "youtube-kids", "youtube-altered", "weibo-content-type", "hupu-zone", ...model.platforms.filter((p) => /^[a-z][a-z0-9_-]*$/.test(p.id)).flatMap((p) => [`${p.id}-title`, `${p.id}-description`])];
  }

  function captureDraft() {
    const fields = {};
    for (const id of draftFields()) { const element = $(`#${id}`); if (element) fields[id] = element.value; }
    return { version: 1, fields, platforms: selectedPlatforms(), assets: Object.fromEntries(assetSlots.filter((slot) => validId(model.assets[slot]?.id)).map((slot) => [slot, model.assets[slot].id])) };
  }

  function draftStatus(message, isError = false) {
    $("#draft-status").textContent = message;
    $("#draft-status").classList.toggle("inline-error", isError);
  }

  function saveDraft() {
    try { window.sessionStorage.setItem(draftStorageKey, JSON.stringify(captureDraft())); draftStatus("已暂存，刷新页面可恢复。"); }
    catch { draftStatus("浏览器未能保存会话草稿。请保留此页面；任务记录仍可恢复已提交的材料。", true); }
  }

  function draftChanged() {
    model.draftRevision += 1;
    $("#validation-panel").hidden = true;
    saveDraft();
    updateActions();
  }

  function readSessionDraft() {
    try {
      const draft = JSON.parse(window.sessionStorage.getItem(draftStorageKey) || "null");
      return draft?.version === 1 && draft.fields && typeof draft.fields === "object" && Array.isArray(draft.platforms) && draft.assets && typeof draft.assets === "object" ? draft : null;
    } catch { return null; }
  }

  function applyDraft(draft, assets) {
    for (const id of draftFields()) {
      const element = $(`#${id}`);
      if (element) element.value = typeof draft.fields?.[id] === "string" ? draft.fields[id] : "";
    }
    const selected = Array.isArray(draft.platforms) ? draft.platforms : [];
    for (const input of $$("input[name='platform']")) input.checked = selected.includes(input.value) && !input.disabled && supportsPrepare(input.value);
    model.assets = assets;
    for (const slot of assetSlots) renderAsset(slot);
    for (const card of $$(".platform-card")) if (selected.includes(card.dataset.platform)) $("details", card).open = true;
    $("#title-count").textContent = `${Array.from($("#title").value).length} 字`;
    model.draftRevision += 1;
    $("#validation-panel").hidden = true;
    updateActions();
  }

  async function restoreSessionDraft() {
    if (model.sessionRestoreDone || !model.sessionDraft) return;
    if (model.draftRevision || model.uploading.size) { model.sessionRestoreDone = true; return; }
    const draft = model.sessionDraft;
    const revision = model.draftRevision;
    const request = ++model.restoreRequest;
    model.restoring = true;
    draftStatus("正在恢复会话草稿并检查本地文件…");
    updateActions();
    try {
      const assets = {}, missing = [];
      const results = await Promise.allSettled(assetSlots.map(async (slot) => {
        const id = draft.assets[slot];
        if (!id) return;
        if (!validId(id)) { missing.push(assetLabels[slot]); return; }
        try {
          const data = await api(`/api/assets/${encodeURIComponent(id)}/info`);
          if (data.asset?.id !== id) throw new Error(`${assetLabels[slot]}信息不完整，请重新检查。`);
          assets[slot] = data.asset;
        } catch (error) { if (error.httpStatus === 404 || error.httpStatus === 410) missing.push(assetLabels[slot]); else throw error; }
      }));
      if (request !== model.restoreRequest || revision !== model.draftRevision) { model.sessionRestoreDone = true; return; }
      const failure = results.find((result) => result.status === "rejected");
      if (failure) throw failure.reason;
      applyDraft(draft, assets);
      if (missing.includes(assetLabels.video)) resetVideoDeclarations();
      model.sessionRestoreDone = true;
      saveDraft();
      const unselected = draft.platforms.filter((id) => !selectedPlatforms().includes(id));
      const notices = [missing.length ? `${missing.join("、")}的本地文件不存在，已移除引用，请重新选择。` : "", unselected.length ? `${unselected.map(platformName).join("、")}当前不支持上传并填写，已取消勾选。` : ""].filter(Boolean);
      if (notices.length) draftStatus(`草稿已恢复；${notices.join(" ")}`, true);
    } catch (error) {
      if (request === model.restoreRequest && revision === model.draftRevision) draftStatus(`会话草稿仍已保存，暂未恢复：${errorMessage(error)} 点击「重新检查」可重试。`, true);
    } finally { if (request === model.restoreRequest) { model.restoring = false; updateActions(); } }
  }

  function payloadDraft(payload, onlyPlatform = null) {
    const fields = { title: payload.title || "", description: payload.description || "", tags: Array.isArray(payload.tags) ? payload.tags.join("，") : "", "ai-generated": typeof payload.ai_generated === "boolean" ? String(payload.ai_generated) : "" };
    for (const platform of model.platforms) {
      if (!/^[a-z][a-z0-9_-]*$/.test(platform.id)) continue;
      fields[`${platform.id}-title`] = payload.overrides?.[platform.id]?.title || "";
      fields[`${platform.id}-description`] = payload.overrides?.[platform.id]?.description || "";
    }
    fields["bilibili-category"] = payload.bilibili?.category || "";
    fields["bilibili-declaration"] = payload.bilibili?.declaration || "";
    fields["youtube-visibility"] = payload.youtube?.visibility || "";
    fields["youtube-kids"] = typeof payload.youtube?.made_for_kids === "boolean" ? String(payload.youtube.made_for_kids) : "";
    fields["youtube-altered"] = typeof payload.youtube?.altered_synthetic === "boolean" ? String(payload.youtube.altered_synthetic) : "";
    fields["weibo-content-type"] = payload.weibo?.content_type || "";
    fields["hupu-zone"] = payload.hupu?.zone || "";
    return { fields, platforms: onlyPlatform ? [onlyPlatform] : payload.platforms || [] };
  }

  function canRetry(result) { return ["failed", "skipped"].includes(result?.status) && result.retry_safe === true; }

  async function restoreJobDraft(id, onlyPlatform = null) {
    if (model.restoring || model.uploading.size || model.submitting) { toast("请等待当前文件上传或材料恢复完成。", true); return; }
    const job = model.jobs.find((item) => item.id === id);
    if (onlyPlatform && !job?.results?.some((result) => result.platform === onlyPlatform && canRetry(result))) { toast("该平台结果尚未确认可以安全重试，请先核验平台。", true); return; }
    if ((Object.keys(model.assets).length || draftFields().some((field) => { const value = $(`#${field}`)?.value; return value && !(field === "youtube-visibility" && value === "private"); })) && !window.confirm("恢复材料将替换当前表单草稿，本地文件不会被删除。是否继续？")) return;
    const revision = model.draftRevision;
    const request = ++model.restoreRequest;
    model.restoring = true;
    updateActions();
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/draft`);
      if (request !== model.restoreRequest || revision !== model.draftRevision) { toast("恢复期间你已修改材料，已保留当前输入。需要时可再次点击「恢复材料」。"); return; }
      if (!data.payload || !data.assets) throw new Error("任务没有可恢复的材料。");
      const assets = {}, missing = [];
      for (const slot of assetSlots) {
        if (validId(data.assets[slot]?.id)) assets[slot] = data.assets[slot];
        else if (data.payload[({ video: "video_id", cover: "cover_id", landscape: "landscape_cover_id" })[slot]]) missing.push(assetLabels[slot]);
      }
      const latest = model.jobs.find((item) => item.id === id);
      if (onlyPlatform && !latest?.results?.some((result) => result.platform === onlyPlatform && canRetry(result))) { toast("该平台状态已变化，请先核对最新结果。", true); return; }
      const draft = payloadDraft(data.payload, onlyPlatform);
      const blocked = onlyPlatform ? [] : draft.platforms.filter((platform) => !latest?.results?.some((result) => result.platform === platform && canRetry(result)));
      draft.platforms = draft.platforms.filter((platform) => !blocked.includes(platform));
      applyDraft(draft, assets);
      if (missing.includes(assetLabels.video)) resetVideoDeclarations();
      model.sessionRestoreDone = true;
      saveDraft();
      const selected = selectedPlatforms();
      const unavailable = (onlyPlatform ? [onlyPlatform] : data.payload.platforms || []).filter((platform) => !selected.includes(platform));
      const message = onlyPlatform ? `已恢复材料并仅选择${platformName(onlyPlatform)}。检查后点击「上传并填写」才会开始重试；最后在保留的浏览器窗口自行发布。` : "材料已恢复，已有本地文件无需重新上传。确认后点击「上传并填写」，最后在保留的浏览器窗口自行发布。";
      const warnings = [missing.length ? `${missing.join("、")}本地文件不存在，请重新选择。` : "", onlyPlatform && !selected.includes(onlyPlatform) ? "该平台当前不可用，未勾选；请查看账号卡说明。" : "", !onlyPlatform && job?.results?.some((result) => ["submitted", "published"].includes(result.status)) ? "原任务包含已提交的平台，已取消这些平台的勾选，避免重复上传或发布。" : "", job?.results?.some((result) => ["needs_attention", "interrupted"].includes(result.status)) || ["needs_attention", "interrupted"].includes(job?.status) ? (onlyPlatform ? "其他平台的待核验结果保持不变，本次不会重试它们。" : "原任务存在待核验结果，请先到平台确认是否已经提交，避免重复发布。") : ""].filter(Boolean);
      if (!onlyPlatform && blocked.length) warnings.push(`${blocked.map(platformName).join("、")}尚未确认可安全重试，已取消勾选。已准备的稿件请切回保留的专用浏览器窗口；待核验结果先查看任务详情。`);
      if (!onlyPlatform && unavailable.filter((platform) => !blocked.includes(platform)).length) warnings.push("当前不可用的平台已取消勾选；请查看平台卡说明。");
      if (job?.results?.some((result) => result.status === "prepared")) warnings.push("原任务已等待你发布，请先使用保留的窗口；如要重新准备，先在任务详情核对并记录「未发布」。");
      $("#recovery-notice").textContent = [message, ...warnings].join(" ");
      $("#recovery-notice").hidden = false;
      if ($("#job-dialog").open) $("#job-dialog").close();
      showView("compose");
      $("#compose").scrollIntoView({ behavior: "smooth", block: "start" });
      toast(onlyPlatform ? "重试材料已准备好，尚未发起发布。" : "已恢复材料，尚未发起发布。");
    } catch (error) { toast(errorMessage(error), true); }
    finally { if (request === model.restoreRequest) { model.restoring = false; updateActions(); } }
  }

  function clearDraft() {
    if (model.uploading.size || model.submitting) return;
    if (!window.confirm("清空当前草稿？标题、平台选择和文件引用会被清空，本地视频、封面和任务记录仍保留。")) return;
    model.restoreRequest += 1;
    model.restoring = false;
    model.sessionRestoreDone = true;
    model.sessionDraft = null;
    applyDraft({ fields: {}, platforms: [] }, {});
    $("#recovery-notice").hidden = true;
    try { window.sessionStorage.removeItem(draftStorageKey); draftStatus("草稿已清空，本地文件和任务记录仍保留。"); }
    catch { draftStatus("表单已清空，但浏览器未能移除会话草稿。刷新时可能再次恢复。", true); }
  }

  // Templates intentionally exclude assets, accounts, per-video declarations and visibility.
  function templateFields() { return ["title", "description", "tags", ...model.platforms.filter((p) => /^[a-z][a-z0-9_-]*$/.test(p.id)).flatMap((p) => [`${p.id}-title`, `${p.id}-description`])]; }

  function readTemplates() {
    const unreadable = () => { $("#template-status").textContent = "部分已存模板格式异常或内容过大，未加载，也没有改写原数据。当前文案不受影响。"; };
    try {
      const raw = window.localStorage.getItem(templateStorageKey) || "[]";
      if (raw.length > 524288) { unreadable(); return []; }
      const stored = JSON.parse(raw);
      if (!Array.isArray(stored) || stored.length > 8) { unreadable(); return []; }
      const allowed = new Set(["title", "description", "tags", ...platformBranding.flatMap((p) => [`${p.id}-title`, `${p.id}-description`])]);
      const result = [], ids = new Set(), names = new Set();
      for (const item of stored) {
        const valid = item && typeof item === "object" && validId(item.id) && item.id.length <= 80 && !ids.has(item.id) && typeof item.name === "string" && item.name.trim() && item.name.length <= 40 && !names.has(item.name) && item.fields && typeof item.fields === "object" && !Array.isArray(item.fields) && Object.keys(item.fields).length <= 32 && Object.entries(item.fields).every(([key, value]) => allowed.has(key) && typeof value === "string" && value.length <= 50000) && Array.isArray(item.platforms) && item.platforms.length <= 32 && item.platforms.every((id) => typeof id === "string" && /^[a-z][a-z0-9_-]{0,63}$/.test(id));
        if (!valid) { unreadable(); continue; }
        ids.add(item.id); names.add(item.name);
        result.push({ id: item.id, name: item.name, fields: { ...item.fields }, platforms: [...item.platforms] });
      }
      return result;
    } catch { unreadable(); return []; }
  }

  function renderTemplates(selected = $("#template-select").value) {
    $("#template-select").innerHTML = '<option value="">选择已保存模板</option>' + model.templates.map((item) => `<option value="${escape(item.id)}">${escape(item.name)}</option>`).join("");
    $("#template-select").value = model.templates.some((item) => item.id === selected) ? selected : "";
    updateActions();
  }

  function writeTemplates(next) {
    const raw = JSON.stringify(next);
    if (raw.length > 524288) { $("#template-status").textContent = "模板内容总量过大，本次没有保存。请减少文案或模板数量；原模板和当前文案保持不变。"; toast("模板内容过多，本次没有保存。", true); return false; }
    try { window.localStorage.setItem(templateStorageKey, raw); model.templates = next; return true; }
    catch { $("#template-status").textContent = "浏览器未能保存模板，请检查存储权限；当前文案仍保留。"; toast("模板没有保存成功，当前文案仍保留。", true); return false; }
  }

  function saveTemplate() {
    const name = $("#template-name").value.trim();
    if (name.length > 40) { toast("模板名称最多 40 字，请缩短后再保存。", true); return; }
    if (!name) { $("#template-name").focus(); toast("给这份常用文案起个名字。", true); return; }
    const fields = Object.fromEntries(templateFields().map((id) => [id, $(`#${id}`)?.value || ""]));
    if (Object.values(fields).some((value) => value.length > 50000)) { toast("单个文案字段超过 50,000 字，请缩短后再保存模板。", true); return; }
    if (!Object.values(fields).some((value) => value.trim())) { toast("先填写想复用的标题、正文或标签。", true); return; }
    const previous = model.templates.find((item) => item.name === name);
    if (previous && !window.confirm(`替换文案模板「${name}」？现有模板内容将被覆盖。`)) return;
    if (!previous && model.templates.length >= 8) { toast("已保存 8 份模板，请删除不用的模板或使用同名替换。", true); return; }
    const item = { id: previous?.id || `copy-${Date.now().toString(36)}-${++model.templateSerial}`, name, fields, platforms: selectedPlatforms() };
    if (!writeTemplates([item, ...model.templates.filter((entry) => entry.id !== item.id)])) return;
    renderTemplates(item.id);
    $("#template-status").textContent = `已保存「${name}」，关闭页面后仍可使用。声明、账号和文件未保存在模板内。`;
    toast("常用文案已保存到这个浏览器。");
  }

  function resetVideoDeclarations() {
    for (const id of ["ai-generated", "bilibili-declaration", "youtube-kids", "youtube-altered", "youtube-visibility", "weibo-content-type"]) { const element = $(`#${id}`); if (element) element.value = ""; }
  }

  function applyTemplate() {
    if (model.restoring || model.uploading.size || model.submitting) return;
    const item = model.templates.find((entry) => entry.id === $("#template-select").value);
    if (!item) return;
    if (!window.confirm(`套用「${item.name}」会替换当前文案和平台选择，保留已选视频与封面。内容声明和 YouTube 可见范围将清空，需要按这条视频重新选择。是否继续？`)) return;
    for (const id of templateFields()) { const element = $(`#${id}`); if (element) element.value = item.fields[id] || ""; }
    for (const input of $$("input[name='platform']")) input.checked = item.platforms.includes(input.value) && !input.disabled && supportsPrepare(input.value);
    resetVideoDeclarations();
    $("#title-count").textContent = `${Array.from($("#title").value).length} 字`;
    for (const card of $$(".platform-card")) if (selectedPlatforms().includes(card.dataset.platform)) $("details", card).open = true;
    draftChanged();
    $("#recovery-notice").textContent = `已套用「${item.name}」。请核对视频、封面和文案，重新选择内容声明及 YouTube 设置。`;
    $("#recovery-notice").hidden = false;
    $("#template-status").textContent = "模板已套用，只更新本地表单，没有发起上传。";
  }

  function deleteTemplate() {
    const item = model.templates.find((entry) => entry.id === $("#template-select").value);
    if (!item || !window.confirm(`删除文案模板「${item.name}」？当前表单和任务不会被删除。`)) return;
    if (writeTemplates(model.templates.filter((entry) => entry.id !== item.id))) { renderTemplates(); $("#template-status").textContent = "模板已删除，当前表单不变。"; }
  }

  function assetSummary(asset) {
    const duration = Number(asset.duration);
    return [formatSize(asset.size), Number(asset.width) > 0 && Number(asset.height) > 0 ? `${asset.width} × ${asset.height}` : "", Number.isFinite(duration) && duration > 0 ? `${Math.floor(duration / 60)}:${String(Math.floor(duration % 60)).padStart(2, "0")}` : ""].filter(Boolean).join(" · ");
  }

  function closeAssetPicker() { model.assetPicker = null; if ($("#asset-picker").open) $("#asset-picker").close(); }

  function renderRecentAssets() {
    const picker = model.assetPicker;
    if (!picker) return;
    $("#asset-picker-list").innerHTML = picker.assets.map((asset) => `<article class="recent-asset">${picker.slot !== "video" && assetLink(asset.url) ? `<img src="${escape(assetLink(asset.url))}" alt="${escape(asset.name || "封面预览")}" loading="lazy">` : ""}<div><strong>${escape(asset.name || "本地素材")}</strong><small>${escape(assetSummary(asset))}</small></div><button type="button" class="button button-small button-secondary" data-use-asset="${escape(asset.id)}" ${picker.selecting ? "disabled" : ""}>${picker.selecting === asset.id ? "检查文件…" : model.assets[picker.slot]?.id === asset.id ? "再次选用" : "选用"}</button></article>`).join("");
    $("#refresh-assets").disabled = Boolean(picker.selecting || picker.loading);
  }

  async function openRecentAssets(slot) {
    if (!assetSlots.includes(slot) || !model.token || model.uploading.size || model.restoring || model.submitting) return;
    const picker = { slot, assets: [], loading: true, selecting: null };
    model.assetPicker = picker;
    $("#asset-picker-title").textContent = `复用本地${assetLabels[slot]}`;
    $("#asset-picker-status").textContent = "正在读取最近保存的素材…";
    renderRecentAssets();
    if (!$("#asset-picker").open) $("#asset-picker").showModal();
    try {
      const kind = slot === "video" ? "video" : "cover";
      const data = await api(`/api/assets?kind=${kind}&limit=12`);
      if (model.assetPicker !== picker) return;
      picker.assets = (Array.isArray(data.assets) ? data.assets : []).filter((asset) => validId(asset?.id) && asset.kind === kind && assetLink(asset.url));
      $("#asset-picker-status").textContent = picker.assets.length ? `最近 ${picker.assets.length} 个可用素材${data.has_more ? "；更早素材可从任务记录恢复" : ""}。` : "还没有可复用的素材。先选择本地文件，上传到本地服务后就能在这里复用。";
    } catch (error) { if (model.assetPicker === picker) $("#asset-picker-status").textContent = `读取失败：${errorMessage(error)} 可以刷新重试。`; }
    finally { if (model.assetPicker === picker) { picker.loading = false; renderRecentAssets(); } }
  }

  async function useRecentAsset(id) {
    const picker = model.assetPicker;
    if (!picker || picker.selecting || picker.loading || !picker.assets.some((asset) => asset.id === id) || model.uploading.size || model.restoring || model.submitting) return;
    picker.selecting = id;
    const revision = model.draftRevision;
    renderRecentAssets();
    try {
      const data = await api(`/api/assets/${encodeURIComponent(id)}/info`);
      if (model.assetPicker !== picker) return;
      if (revision !== model.draftRevision || model.uploading.size) { $("#asset-picker-status").textContent = "检查期间材料已修改，已保留当前输入。需要时请再次选用。"; return; }
      const asset = data.asset;
      if (asset?.id !== id || asset.kind !== (picker.slot === "video" ? "video" : "cover") || !assetLink(asset.url)) throw new Error("素材信息不完整或类型不符，请刷新后重试。");
      const changedVideo = picker.slot === "video" && model.assets.video && model.assets.video.id !== id;
      model.assets[picker.slot] = asset;
      if (changedVideo) resetVideoDeclarations();
      renderAsset(picker.slot);
      draftChanged();
      closeAssetPicker();
      toast(changedVideo ? "已切换视频，请按新视频重新选择内容声明和可见范围。" : `已复用${assetLabels[picker.slot]}，无需再次上传到本地。`);
    } catch (error) {
      if (model.assetPicker !== picker) return;
      if ([404, 410].includes(error.httpStatus)) picker.assets = picker.assets.filter((asset) => asset.id !== id);
      $("#asset-picker-status").textContent = `未能选用：${errorMessage(error)} 当前文件保持不变。`;
    } finally { if (model.assetPicker === picker) { picker.selecting = null; renderRecentAssets(); const button = picker.assets.some((asset) => asset.id === id) ? $(`[data-use-asset="${id}"]`) : $("#refresh-assets"); button?.focus?.(); } }
  }

  function editorIssues(payload) {
    const issues = [], selected = payload.platforms || [];
    const add = (target, message, platform = null) => issues.push({ target, message, platform });
    if (!payload.video_id) add("video-file", "选择视频");
    if (!payload.cover_id) add("cover-file", "选择主封面");
    if (!selected.length) add("platform-list", "选择发布平台");
    if (typeof payload.ai_generated !== "boolean") add("ai-generated", "选择 AI 内容声明");
    if (!payload.title && selected.some((id) => !payload.overrides?.[id]?.title)) add("title", "填写标题");
    for (const id of selected) {
      const title = payload.overrides?.[id]?.title || payload.title || "";
      const limit = { douyin: 30, xiaohongshu: 20, youtube: 100, bilibili: 80 }[id];
      if (limit && Array.from(title).length > limit) add(`${id}-title`, `${platformName(id)}标题需缩短至 ${limit} 字`, id);
      const description = payload.overrides?.[id]?.description || payload.description || "";
      const fullDescription = description + (payload.tags?.length ? "\n" + payload.tags.map((tag) => `#${tag}`).join(" ") : "");
      const descLimit = { douyin: 1000, xiaohongshu: 1000, youtube: 5000, bilibili: 2000 }[id];
      if (descLimit && Array.from(fullDescription).length > descLimit) add(`${id}-description`, `${platformName(id)}正文与标签超过 ${descLimit} 字`, id);
      if (id === "xiaohongshu" && payload.ai_generated === true) add("platform-xiaohongshu", "小红书 AI 声明暂未接通，请取消该平台", id);
    }
    for (const id of selected) {
      const platform = model.platforms.find(p => p.id === id);
      if (payload.browser_mode !== "chrome" && platform?.account_status && accountInfo(platform.account_status)[1] !== "verified") {
        const retry = ["verification_unavailable","error","pending_verification"].includes(accountInfo(platform.account_status)[1]);
        add(`account-card-${id}`, `${retry ? "检查" : "连接"}${platformName(id)}账号`, id);
      }
    }
    if (payload.browser_mode === "chrome" && model.nativeChrome && !model.nativeChrome.connected) add("native-chrome-connect", "连接普通 Chrome 扩展");
    if (selected.includes("youtube")) {
      if (!["private", "unlisted", "public"].includes(payload.youtube?.visibility)) add("youtube-visibility", "选择 YouTube 可见范围", "youtube");
      if (typeof payload.youtube?.made_for_kids !== "boolean") add("youtube-kids", "选择 YouTube 儿童受众", "youtube");
      if (typeof payload.youtube?.altered_synthetic !== "boolean") add("youtube-altered", "选择 YouTube 合成内容声明", "youtube");
    }
    if (selected.includes("bilibili")) { if (!payload.tags?.length) add("tags", "填写B站标签", "bilibili"); if (!payload.bilibili?.category) add("bilibili-category", "选择B站分区", "bilibili"); if (!payload.bilibili?.declaration) add("bilibili-declaration", "选择B站创作声明", "bilibili"); }
    if (selected.includes("weibo") && !payload.weibo?.content_type) add("weibo-content-type", "选择微博内容类型", "weibo");
    if (selected.includes("hupu") && !payload.hupu?.zone) add("hupu-zone", "选择虎扑专区", "hupu");
    return issues;
  }

  function focusField(id) {
    if (!/^[a-z][a-z0-9_-]*$/.test(id)) return;
    const element = $(`#${id}`);
    if (!element) return;
    showView(id.startsWith("account-card-") ? "accounts" : "compose");
    for (let details = element.closest?.("details"); details; details = details.parentElement?.closest?.("details")) details.open = true;
    const zone = element.closest?.(".upload-zone");
    const target = zone ? $(".choose-file", zone) || $(".remove-file", zone) : element;
    const card = element.closest?.(".platform-card");
    (card || zone || element).scrollIntoView({ behavior: "smooth", block: "center" });
    const focus = id === "platform-list" ? $("input[name='platform']:not(:disabled)", element) : target;
    focus?.focus?.({ preventScroll: true });
  }

  function renderReadiness(payload, issues) {
    $("#readiness-summary").textContent = issues.length ? `还需完善 ${issues.length} 项，点击可直接定位。` : "必填项已齐。检查发布材料后，再开始上传填写。";
    $("#readiness-items").innerHTML = issues.length ? issues.map((issue) => `<button type="button" class="readiness-item missing" data-focus-field="${escape(issue.target)}">${escape(issue.message)} <span>去完善 ↗</span></button>`).join("") : '<span class="readiness-item ready">视频、封面、标题与必选声明已填写</span>';
    for (const platform of model.platforms) {
      const element = $(`[data-platform-next="${platform.id}"]`);
      if (!element) continue;
      element.hidden = !(payload.platforms || []).includes(platform.id);
      const issue = issues.find((entry) => entry.platform === platform.id) || issues.find((entry) => !entry.platform);
      const title = payload.overrides?.[platform.id]?.title || payload.title || "";
      const account = payload.browser_mode === "chrome" ? ["当前 Chrome 账号"] : accountInfo(platform.account_status);
      element.innerHTML = issue ? `<button type="button" class="text-button" data-focus-field="${escape(issue.target)}">下一步：${escape(issue.message)} ↗</button>` : `文案 ${Array.from(title).length} 字 · ${platform.id === "youtube" && payload.landscape_cover_id ? "使用横版封面" : "使用主封面"} · ${escape(account[0])}`;
    }
  }

  let platformSignature = "";
  function platformSettings(id) {
    if (id === "bilibili") { const platform = model.platforms.find(p => p.id === id) || {}; return `<label class="field-label" for="bilibili-category">B站分区 <span class="required">必填</span></label><select id="bilibili-category"><option value="">请选择分区</option>${(platform.categories || []).map(name => `<option value="${escape(name)}">${escape(name)}</option>`).join("")}</select><label class="field-label" for="bilibili-declaration">B站创作声明 <span class="required">必填</span></label><select id="bilibili-declaration"><option value="">请选择创作声明</option>${Object.entries(platform.declarations || {}).map(([key,name]) => `<option value="${escape(key)}">${escape(name)}</option>`).join("")}</select><p class="field-help">按实际内容选择。指定封面同时设置4:3和16:9比例，发布前请检查裁切。</p>`; }
    if (id === "youtube") return `<label class="field-label" for="youtube-visibility">可见范围</label><select id="youtube-visibility"><option value="">请选择可见范围</option><option value="private">私享（Private）</option><option value="unlisted">不公开（Unlisted）</option><option value="public">公开（Public）</option></select><label class="field-label" for="youtube-kids">是否为儿童打造 <span class="required">必选</span></label><select id="youtube-kids"><option value="">请选择受众设置</option><option value="false">否，不是为儿童打造</option><option value="true">是，为儿童打造</option></select><label class="field-label" for="youtube-altered">是否含逼真的修改或合成内容 <span class="required">必选</span></label><select id="youtube-altered"><option value="">请选择合成内容声明</option><option value="false">否</option><option value="true">是</option></select><p class="field-help">按这条视频的实际情况选择受众和合成内容声明。</p>`;
    if (id === "weibo") return `<label class="field-label" for="weibo-content-type">微博内容类型 <span class="required">必选</span></label><select id="weibo-content-type"><option value="">请选择内容类型</option><option value="original">原创</option><option value="recreation">二创</option></select>`;
    if (id === "hupu") return `<label class="field-label" for="hupu-zone">虎扑发布专区 <span class="required">必选</span></label><select id="hupu-zone"><option value="">请选择发布专区</option><option value="walking_street">步行街主干道</option></select><p class="field-help">当前适配支持步行街主干道；标题需 4–40 字。</p>`;
    if (id === "tencent") return '<p class="field-help">视频号短标题需 6–16 字，可在上方填写单独标题。</p>';
    return "";
  }

  function showView(requested, { updateHash = true } = {}) {
    const view = ["compose", "accounts", "history"].includes(requested) ? requested : "compose";
    const changed = model.view !== view;
    model.view = view;
    for (const section of $$("[data-view]")) section.hidden = section.dataset.view !== view;
    for (const link of $$(".nav-item")) {
      const active = link.getAttribute("href") === `#${view}`;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    }
    const title = {compose: "发视频", accounts: "账号管理", history: "任务记录"}[view];
    $("#view-title").textContent = title;
    document.title = `${title} · 发布台`;
    if (updateHash && window.location.hash !== `#${view}`) {
      if (window.history?.pushState) window.history.pushState(null, "", `#${view}`);
      else window.location.hash = view;
    }
    if (changed) window.scrollTo?.({top:0,behavior:"instant"});
  }

  function renderAccounts(platforms = model.platforms) {
    const card = (platform) => {
      const id = platform.id, name = escape(platform.name || id);
      const icon = escape(platformBranding.find(p => p.id === id)?.icon || name[0]);
      if (model.nativeChrome && ["douyin","xiaohongshu","bilibili"].includes(id)) return `<article class="account-card" id="account-card-${id}" data-account-platform="${id}"><h2>${name}</h2><p data-native-account="${id}">${model.nativeChrome.connected ? "使用普通 Chrome 当前账号" : "请先连接普通 Chrome 扩展"}</p><p>开始任务后直接打开官方创作页面，使用该 Chrome 的现有登录。需要验证时在对应页面完成；最终发布由你点击。</p><button type="button" class="button button-secondary" data-focus-field="native-chrome-connect">查看 Chrome 连接</button></article>`;
      return `<article class="account-card" id="account-card-${id}" data-account-platform="${id}">
        <div class="account-card-top"><div class="account-identity"><span class="platform-icon ${id}" aria-hidden="true">${icon}</span><div><h2>${name}</h2><span class="account-status" data-account-status="${id}">读取状态…</span></div></div></div>
        <p class="account-context" data-account-context="${id}"></p><p class="account-message" data-account-message="${id}" hidden></p>
        <div class="account-actions"><button type="button" class="button button-primary account-primary" data-account-primary="${id}" data-account-action="login" data-platform="${id}">连接账号</button><button type="button" class="button button-ghost" data-account-log="${id}" hidden>连接记录</button></div>
        <details class="account-details"><summary>连接方式与平台说明</summary><p class="workflow-status" data-workflow-status="${id}"></p><p class="platform-limitations" data-platform-limitations="${id}"></p><p class="capability-note" data-capabilities="${id}"></p>
        <div class="account-actions">${supportsChrome(id) ? `<button type="button" class="button button-small button-secondary" data-account-action="chrome" data-platform="${id}">从 Chrome 导入（可选）</button>` : ""}<button type="button" class="button button-small button-ghost" data-account-action="login" data-platform="${id}">${id === "bilibili" ? "App 扫码授权" : "直接登录"}</button><button type="button" class="button button-small button-ghost" data-account-action="check" data-platform="${id}">检查状态</button></div></details>
      </article>`;
    };
    $("#account-list").innerHTML = platforms.filter(p => supportsPrepare(p.id)).map(card).join("") || '<div class="empty-state"><strong>正在读取账号</strong><p>连接本机工作区后，账号会显示在这里。</p></div>';
    $("#unsupported-account-list").innerHTML = platforms.filter(p => !supportsPrepare(p.id)).map(card).join("");
  }

  function renderPlatforms() {
    const platforms = model.platforms.filter(platform => /^[a-z][a-z0-9_-]*$/.test(platform.id));
    const signature = JSON.stringify(platforms.map(platform => [platform.id, platform.name, platform.capabilities, platform.disabled, platform.reason, platform.available, platform.workflow_status, supportsChrome(platform.id), supportsPrepare(platform.id), platform.categories, platform.declarations,Boolean(model.nativeChrome)]));
    if (signature === platformSignature) return;
    platformSignature = signature;
    const values = new Map($$("#platform-list input, #platform-list textarea, #platform-list select").map(element => [element.id || `platform-${element.value}`, { value: element.value, checked: element.checked }]));
    const expanded = new Set($$(".platform-card > details[open]").map(element => element.closest(".platform-card").dataset.platform));
    const available = platforms.filter(platform => supportsPrepare(platform.id));
    $("#platform-list").innerHTML = available.length ? available.map(platform => {
      const id = platform.id, name = escape(platform.name || id);
      const icon = escape(platformBranding.find(item => item.id === id)?.icon || Array.from(platform.name || id)[0]);
      const settings = platformSettings(id);
      return `<article class="platform-card${isDisabled(platform) ? " disabled" : ""}" data-platform="${id}">
        <div class="platform-card-header"><label class="platform-select"><span class="platform-icon ${id}" aria-hidden="true">${icon}</span><span><span class="platform-name">${name}</span><span class="account-status" data-compose-status="${id}">读取连接…</span></span><input type="checkbox" id="platform-${id}" name="platform" value="${id}" aria-label="上传并填写到${name}" ${isDisabled(platform) ? "disabled" : ""}></label></div>
        <p class="platform-next-step" data-platform-next="${id}" hidden></p>
        <details class="platform-options"><summary>平台设置</summary>${settings ? `<div class="platform-required" hidden>${settings}</div>` : ""}<details class="copy-overrides"><summary>单独文案 <span>可选</span></summary><label class="field-label" for="${id}-title">单独标题</label><input id="${id}-title" placeholder="留空使用通用标题" data-override="title" autocomplete="off"><label class="field-label" for="${id}-description">单独正文 / 简介</label><textarea id="${id}-description" rows="3" placeholder="留空使用通用正文" data-override="description"></textarea></details></details>
      </article>`;
    }).join("") : '<p class="platform-empty">正在读取可用平台…</p>';
    renderAccounts(platforms);
    for (const element of $$("#platform-list input, #platform-list textarea, #platform-list select")) {
      const prior = values.get(element.id || `platform-${element.value}`);
      if (prior) { if (element.type === "checkbox") element.checked = prior.checked && !element.disabled; else element.value = prior.value; }
    }
    for (const card of $$(".platform-card")) if (expanded.has(card.dataset.platform)) $("details", card).open = true;
    updateActions();
  }

  function accountInfo(value) {
    const status = typeof value === "object" && value ? (value.status || value.state) : value;
    if (["verified", "valid", "logged_in", "authenticated", "ready"].includes(status)) return ["登录已保存", "verified"];
    if (["imported", "pending_verification"].includes(status)) return ["已同步 · 待在线核验", "pending_verification"];
    if (status === "not_connected") return ["未连接", "not_connected"];
    if (["needs_login", "login_required", "expired", "not_logged_in", "missing", "invalid"].includes(status)) return ["需要重新登录", "needs_login"];
    if (status === "verification_unavailable") return ["暂时无法检查", "verification_unavailable"];
    if (["error", "failed", "check_failed"].includes(status)) return ["检查失败", "error"];
    if (["checking", "running", "logging_in"].includes(status)) return ["正在检查 / 登录", "running"];
    return ["待连接", "unverified"];
  }

  function updatePlatformState() {
    for (const platform of model.platforms) {
      const id = platform.id;
      if (model.nativeChrome && ["douyin","xiaohongshu","bilibili"].includes(id)) {
        const status = model.nativeChrome.connected ? "当前 Chrome 账号" : "扩展未连接";
        const compose = $(`[data-compose-status="${id}"]`); if (compose) compose.textContent = status;
        const account = $(`[data-native-account="${id}"]`); if (account) account.textContent = status;
        continue;
      }
      let [label, kind] = accountInfo(platform.account_status);
      if (id === "youtube" && platform.account_source === "chrome" && ["needs_login","error"].includes(kind)) label = "未通过在线核验";
      const busy = model.accountBusy.has(id) || Boolean(latestAccountJob(id) && runningStatuses.has(latestAccountJob(id).status));
      const badge = $(`[data-account-status="${id}"]`);
      if (badge) { badge.textContent = busy ? "正在连接…" : label; badge.className = `account-status ${busy ? "running" : kind}`; }
      const compose = $(`[data-compose-status="${id}"]`);
      if (compose) { compose.textContent = busy ? "正在检查…" : ({verified:"已连接",verification_unavailable:"暂时无法检查",needs_login:"需要登录",not_connected:"未连接",pending_verification:"正在核验",error:"检查未完成",unverified:"待连接"}[kind] || label); compose.className = `account-status ${kind}`; }
      const context = $(`[data-account-context="${id}"]`);
      if (context) {
        const verifiedAt = platform.last_verified_at || (kind === "verified" ? platform.checked_at : null);
        context.textContent = kind === "verified" ? `${platform.account_hint ? platform.account_hint + " · " : ""}登录已保存在本机${verifiedAt ? ` · 最近验证 ${formatDate(verifiedAt)}` : ""}` : kind === "verification_unavailable" ? "已保存的登录仍保留，请稍后重试检查。" : id === "bilibili" ? "点击连接后，在本页用哔哩哔哩 App 扫码。" : "在官方页面完成登录，无需安装扩展。";
      }
      const message = $(`[data-account-message="${id}"]`);
      if (message) { message.textContent = platform.account_message || ""; message.hidden = !message.textContent || kind === "verified"; message.classList.toggle("account-warning", ["needs_login","error","verification_unavailable"].includes(kind)); }
      const primary = $(`[data-account-primary="${id}"]`);
      if (primary) { primary.dataset.accountAction = kind === "verified" && supportsPrepare(id) ? "use" : ["verified","verification_unavailable","error","pending_verification"].includes(kind) ? "check" : "login"; primary.classList.toggle("button-primary", kind !== "verified"); primary.classList.toggle("button-secondary", kind === "verified"); primary.textContent = busy ? "正在处理…" : kind === "verified" ? (supportsPrepare(id) ? "去发视频" : "检查连接") : ["verification_unavailable","error","pending_verification"].includes(kind) ? "重试检查" : kind === "needs_login" ? "重新登录" : "连接账号"; }
      const limitations = $(`[data-platform-limitations="${id}"]`);
      if (limitations) limitations.textContent = [...new Set([platform.note,...(Array.isArray(platform.known_limitations) ? platform.known_limitations : [platform.known_limitations])].filter(x => typeof x === "string" && x.trim()))].join("；");
      const workflow = $(`[data-workflow-status="${id}"]`);
      if (workflow) workflow.textContent = ({implemented:"上传填写已接入 · 最后由本人确认",unverified:"已接入，实际平台流程仍需核验",fixture_only:"上传准备暂未开放",manual_only:"上传准备暂未接入",unsupported:"暂不可用"})[platform.workflow_status] || "";
      const cap = $(`[data-capabilities="${id}"]`);
      if (cap) cap.textContent = [platform.capabilities?.cover === false ? "暂不支持自定义封面" : "",platform.capabilities?.description === false ? "暂不支持正文" : "",platform.capabilities?.tags === false ? "暂不支持标签" : "",platform.capabilities?.note || ""].filter(Boolean).join("；");
      const log = $(`[data-account-log="${id}"]`); if (log) log.hidden = !latestAccountJob(id) && !platform.login_job_id;
      for (const button of $$(`[data-account-action][data-platform="${id}"]`)) button.disabled = !model.token || model.maintenance.draining || isDisabled(platform) || busy;
    }
    $("#account-count").textContent = model.platforms.filter(p => p.session_saved || accountInfo(p.account_status)[1] === "verified").length;
  }

  function latestAccountJob(platform) {
    return model.jobs.find((job) => job.kind !== "publish" && job.kind !== "upload" && (job.platform === platform || job.platform_id === platform || job.platforms?.includes(platform) || job.results?.some((result) => result.platform === platform)));
  }

  function renderRuntime(connectionError = null) {
    const banner = $("#runtime-banner");
    const runtime = model.runtime;
    const draining = model.maintenance.draining;
    banner.className = `runtime-banner ${connectionError ? "error" : draining ? "unavailable" : runtime?.ready ? "ready" : "unavailable"}`;
    $("#runtime-title").textContent = connectionError ? "暂时无法连接本地服务" : draining ? "正在安全维护" : runtime?.ready ? "工作区已就绪" : "发布环境尚未就绪";
    $("#runtime-message").textContent = connectionError || (draining ? `正在等待 ${model.maintenance.active_jobs || 0} 个已有任务结束，暂不接受发布和账号操作。你可以继续编辑或恢复材料。` : runtime?.message || (runtime?.ready ? "请查看各平台的账号核验结果与适配限制，再决定是否发布。" : "材料可以先准备；请按照启动说明完成本地运行环境配置。"));
    updateActions();
  }

  function selectedPlatforms() { return $$("input[name='platform']:checked:not(:disabled)").map((input) => input.value).filter(supportsPrepare); }

  function updateActions() {
    const selected = selectedPlatforms();
    const uploading = model.uploading.size > 0;
    const payload = collectPayload();
    const issues = editorIssues(payload);
    renderReadiness(payload, issues);
    const available = model.platforms.filter((platform) => supportsPrepare(platform.id) && !isDisabled(platform)).map((platform) => platformName(platform.id));
    $("#workflow-platforms").textContent = available.length ? `${available.length} 个可用平台 · 可多选` : "连接工作区后显示可用平台";
    $("#selected-count").textContent = `已选 ${selected.length}`;
    $("#publish-summary").textContent = selected.length ? `将上传并填写：${selected.map(platformName).join("、")}` : "选择要准备的平台";
    $("#publish-hint").textContent = model.maintenance.draining ? "正在安全维护，暂不接受新任务；材料可继续编辑和恢复。" : model.restoring ? "正在恢复材料，请稍候。" : uploading ? "文件正在上传到本地服务，请等待上传完成。" : model.submitting ? "正在创建任务，请勿重复提交。" : !model.runtime?.ready ? "执行环境就绪后可开始；仍可准备材料并检查。" : "自动填好材料，最后由你点击发布。";
    $("#publish-button").disabled = !model.token || !model.runtime?.ready || model.maintenance.draining || model.restoring || model.submitting || model.validating || uploading || issues.length > 0;
    $("#validate-button").disabled = !model.token || model.restoring || model.validating || model.submitting || uploading;
    $("#clear-draft").disabled = uploading || model.submitting;
    for (const button of $$("[data-recent-slot]")) button.disabled = !model.token || uploading || model.restoring || model.submitting;
    $("#apply-template").disabled = !$("#template-select").value || uploading || model.restoring || model.submitting;
    $("#delete-template").disabled = !$("#template-select").value;
    for (const button of $$("[data-restore-job]")) button.disabled = model.restoring || uploading || model.submitting;
    $("#validate-button").textContent = model.validating ? "检查中…" : "检查发布材料";
    $("#publish-button").innerHTML = model.submitting ? "正在创建任务…" : '上传并填写 <span aria-hidden="true">↗</span>';
    for (const card of $$(".platform-card")) { const chosen = selected.includes(card.dataset.platform); card.classList.toggle("selected", chosen); const settings = $(".platform-required", card); if (settings) settings.hidden = !chosen; }
  }

  function collectPayload() {
    const payload = { mode: "prepare", browser_mode: selectedPlatforms().every(p => ["douyin","xiaohongshu","bilibili"].includes(p)) ? "chrome" : "dedicated", video_id: model.assets.video?.id || null, cover_id: model.assets.cover?.id || null, landscape_cover_id: model.assets.landscape?.id || null, title: $("#title").value.trim(), description: $("#description").value, tags: Array.from(new Set($("#tags").value.split(/[,，、\r\n]+|\s+(?=#+[^\s#])/u).map((tag) => tag.trim().replace(/^#+/, "").trim()).filter(Boolean))), platforms: selectedPlatforms(), overrides: {} };
    for (const id of payload.platforms) {
      const title = $(`#${id}-title`).value.trim();
      const description = $(`#${id}-description`).value;
      if (title || description.trim()) { payload.overrides[id] = {}; if (title) payload.overrides[id].title = title; if (description.trim()) payload.overrides[id].description = description; }
    }
    const aiGenerated = $("#ai-generated").value; if (aiGenerated !== "") payload.ai_generated = aiGenerated === "true";
    if (payload.platforms.includes("weibo")) payload.weibo = { content_type: $("#weibo-content-type").value };
    if (payload.platforms.includes("hupu")) payload.hupu = { zone: $("#hupu-zone").value };
    if (payload.platforms.includes("bilibili")) payload.bilibili = {category: $("#bilibili-category").value, declaration: $("#bilibili-declaration").value};
    if (payload.platforms.includes("youtube")) { payload.youtube = { visibility: $("#youtube-visibility").value }; const kids = $("#youtube-kids").value; if (kids !== "") payload.youtube.made_for_kids = kids === "true"; const altered = $("#youtube-altered").value; if (altered !== "") payload.youtube.altered_synthetic = altered === "true"; }
    return payload;
  }

  function localErrors(payload) {
    const errors = [];
    if (!payload.video_id) errors.push("请先选择视频并等待上传完成。");
    if (!payload.cover_id) errors.push("请上传主封面（JPG 或 PNG）。");
    if (!payload.title && payload.platforms.some((id) => !payload.overrides[id]?.title)) errors.push("请填写通用标题，或为各个已选平台填写单独标题。");
    if (typeof payload.ai_generated !== "boolean") errors.push("请明确选择是否含 AI 生成或合成内容。");
    if (!payload.platforms.length) errors.push("请至少选择一个发布平台。");
    if (payload.platforms.includes("bilibili") && (!payload.bilibili.category || !payload.bilibili.declaration)) { errors.push("请选择 B站分区和创作声明。"); $("[data-platform='bilibili'] details").open = true; }
    if (payload.platforms.includes("youtube") && typeof payload.youtube.made_for_kids !== "boolean") { errors.push("请明确选择 YouTube 视频是否为儿童打造。"); $("[data-platform='youtube'] details").open = true; }
    if (payload.platforms.includes("youtube") && typeof payload.youtube.altered_synthetic !== "boolean") { errors.push("请明确选择 YouTube 是否含逼真的修改或合成内容。"); $("[data-platform='youtube'] details").open = true; }
    if (payload.platforms.includes("weibo") && !payload.weibo.content_type) { errors.push("请选择微博的原创 / 二创内容类型。"); $("[data-platform='weibo'] details").open = true; }
    if (payload.platforms.includes("hupu") && !payload.hupu.zone) { errors.push("请选择虎扑发布专区。"); $("[data-platform='hupu'] details").open = true; }
    if (payload.platforms.includes("youtube") && !["private", "unlisted", "public"].includes(payload.youtube?.visibility)) { errors.push("请选择 YouTube 可见范围。"); $("[data-platform='youtube'] details").open = true; }
    return errors;
  }

  function normalizeIssue(issue) {
    if (typeof issue === "string") return issue;
    if (!issue || typeof issue !== "object") return String(issue);
    return `${issue.platform ? `${platformName(issue.platform)}：` : ""}${issue.message || issue.error || issue.detail || JSON.stringify(issue)}`;
  }

  function showValidation(data) {
    const panel = $("#validation-panel");
    panel.hidden = false;
    panel.classList.toggle("invalid", !data.valid);
    panel.innerHTML = `<h3>${data.valid ? "材料检查通过" : "还有材料需要完善"}</h3>${data.valid ? "<p>这里只检查发布材料；没有创建发布任务，也不代表平台已接收。</p>" : ""}${data.errors?.length ? `<ul>${data.errors.map((error) => `<li>${escape(normalizeIssue(error))}</li>`).join("")}</ul>` : ""}${data.warnings?.length ? `<p>请留意：</p><ul>${data.warnings.map((warning) => `<li>${escape(normalizeIssue(warning))}</li>`).join("")}</ul>` : ""}${data.platforms?.length ? `<div class="validation-platforms">${data.platforms.map((platform) => `<span>${escape(platformName(platform.id || platform.platform))} · ${platform.cover_used ? "使用封面" : "未使用自定义封面"}</span>`).join("")}</div>` : ""}`;
  }

  async function validate() {
    if (model.restoring || model.validating || model.submitting || model.uploading.size) return;
    model.validating = true;
    updateActions();
    let payload;
    try {
      payload = collectPayload();
      const data = await api("/api/validate", { method: "POST", body: payload });
      if (model.uploading.size || JSON.stringify(payload) !== JSON.stringify(collectPayload())) { toast("材料已修改，请重新检查当前材料。"); return; }
      showValidation(data);
      if (!data.valid) localErrors(payload);
      $("#validation-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) { if (!payload || JSON.stringify(payload) === JSON.stringify(collectPayload())) showValidation({ valid: false, errors: error.errors?.length ? error.errors : [errorMessage(error)] }); }
    finally { model.validating = false; updateActions(); }
  }

  async function publish(event) {
    event.preventDefault();
    if (model.maintenance.draining || model.restoring || model.submitting || model.validating || model.uploading.size) return;
    const payload = { ...collectPayload(), mode: "prepare" };
    const errors = localErrors(payload);
    if (errors.length) { showValidation({ valid: false, errors }); $("#validation-panel").scrollIntoView({ behavior: "smooth", block: "nearest" }); return; }
    const details = payload.platforms.map((id) => `${platformName(id)}：${payload.overrides[id]?.title || payload.title}`).join("\n");
    if (!window.confirm(`确认在以下 ${payload.platforms.length} 个平台上传并填写？\n\n${details}\n\n将上传视频、填写材料，并保留浏览器窗口。最后请你在窗口里检查内容、封面和声明，再亲自点击发布。工具不会点击最终发布。`)) return;
    model.submitting = true;
    updateActions();
    try {
      const data = await api("/api/jobs", { method: "POST", body: payload });
      if (!data.job?.id) throw new Error("服务未返回任务编号，请先刷新记录确认是否已创建，避免重复提交。");
      upsertJob(data.job);
      renderJobs();
      toast("上传填写任务已创建。出现「待你发布」后，请在保留的浏览器窗口里检查并自行发布。");
      showView("history");
      $("#history").scrollIntoView({ behavior: "smooth", block: "start" });
      await refreshState({ quiet: true });
    } catch (error) { showValidation({ valid: false, errors: error.errors?.length ? error.errors : [errorMessage(error)] }); toast(errorMessage(error), true); }
    finally { model.submitting = false; updateActions(); }
  }

  async function uploadFile(slot, file) {
    if (!file) return;
    if (model.uploading.has(slot)) { toast("这个位置的文件正在上传，请等待完成后再更换。", true); return; }
    const zone = $(`.upload-zone[data-slot="${slot}"]`);
    const errorElement = $(".upload-error", zone);
    errorElement.hidden = true;
    if (!model.token) { errorElement.textContent = "请先连接本地服务后再选择文件。"; errorElement.hidden = false; return; }
    const validImage = /\.(jpe?g|png)$/i.test(file.name) || ["image/jpeg", "image/png"].includes(file.type);
    if (slot !== "video" && !validImage) { errorElement.textContent = "封面请选择 JPG 或 PNG 图片。"; errorElement.hidden = false; return; }
    if (slot === "video" && !/\.(mp4|mov|webm|m4v)$/i.test(file.name)) { errorElement.textContent = "请选择 MP4、MOV、M4V 或 WebM 视频文件。"; errorElement.hidden = false; return; }
    model.uploading.add(slot);
    model.draftRevision += 1;
    const progress = $(".upload-progress", zone);
    progress.hidden = false;
    $("progress", zone).value = 0;
    $(".progress-number", zone).textContent = "0%";
    $(".progress-caption", zone).textContent = "正在上传到本地服务";
    $(".file-input", zone).disabled = true;
    $("#validation-panel").hidden = true;
    for (const button of $$("button", zone)) button.disabled = true;
    updateActions();
    try {
      const asset = await new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        request.open("POST", apiURL("/api/assets"));
        request.setRequestHeader("X-Publisher-Token", model.token);
        request.responseType = "json";
        request.upload.addEventListener("progress", (event) => { if (event.lengthComputable) { const percent = Math.round(event.loaded / event.total * 100); $("progress", zone).value = percent; $(".progress-number", zone).textContent = `${percent}%`; $(".progress-caption", zone).textContent = percent === 100 ? "本地服务正在处理…" : "正在上传到本地服务"; } });
        request.addEventListener("load", () => { const data = request.response; if (request.status >= 200 && request.status < 300 && data?.asset?.id) resolve(data.asset); else reject(new Error(data?.error || `文件上传失败（HTTP ${request.status}）`)); });
        request.addEventListener("error", () => reject(new Error("文件上传中断，请检查本地服务后重试。")));
        request.addEventListener("abort", () => reject(new Error("文件上传已取消。")));
        const body = new FormData(); body.append("file", file); body.append("kind", slot === "video" ? "video" : "cover"); request.send(body);
      });
      const changedVideo = slot === "video" && model.assets.video && model.assets.video.id !== asset.id;
      model.assets[slot] = asset;
      if (changedVideo) { resetVideoDeclarations(); toast("已切换视频，请按新视频重新选择内容声明和可见范围。"); }
      renderAsset(slot);
      draftChanged();
    } catch (error) { errorElement.textContent = errorMessage(error) + (model.assets[slot] ? " 当前仍保留之前的文件。" : ""); errorElement.hidden = false; }
    finally { model.uploading.delete(slot); progress.hidden = true; for (const button of $$("button", zone)) button.disabled = false; $(".file-input", zone).disabled = false; $(".file-input", zone).value = ""; updateActions(); }
  }

  function removeAsset(slot) {
    if (!assetSlots.includes(slot) || model.uploading.has(slot) || model.submitting) return;
    delete model.assets[slot];
    if (slot === "video") resetVideoDeclarations();
    renderAsset(slot);
    draftChanged();
  }

  function renderAsset(slot) {
    const zone = $(`.upload-zone[data-slot="${slot}"]`);
    const asset = model.assets[slot];
    $(".upload-placeholder", zone).hidden = Boolean(asset);
    $(".asset-preview", zone).hidden = !asset;
    const video = $("video", zone);
    if (video) {
      const url = assetLink(asset?.url);
      if (video.getAttribute?.("src") !== url) { video.pause?.(); if (url) video.src = url; else video.removeAttribute("src"); const notice = $(".video-preview-notice", zone); if (notice) notice.hidden = true; }
      video.hidden = !url;
      const icon = $(".video-file-icon", zone); if (icon) icon.hidden = Boolean(url);
    }
    if (!asset) { const image = $("img", zone); if (image) image.removeAttribute("src"); return; }
    $(".asset-name", zone).textContent = asset.name || "已上传文件";
    $(".asset-meta", zone).textContent = `${assetSummary(asset)} · 已保存到本地服务`;
    const image = $("img", zone);
    if (image) { const url = assetLink(asset.url); if (url) image.src = url; else image.removeAttribute("src"); }
  }

  function videoPreviewUnavailable() {
    const zone = $('.upload-zone[data-slot="video"]');
    const notice = $(".video-preview-notice", zone);
    if (!model.assets.video || !notice) return;
    notice.textContent = "当前浏览器无法预览此视频（可能是编码或文件读取问题）。已保留选中素材；请检查本地文件，并点击「检查发布材料」核对。";
    notice.hidden = false;
  }

  async function accountAction(platform, action) {
    if (action === "use") {
      if (!supportsPrepare(platform)) return;
      showView("compose");
      const input = $(`#platform-${platform}`);
      if (input && !input.disabled) { input.checked = true; const settings = input.closest?.(".platform-card")?.querySelector?.("details"); if (settings && ["bilibili","youtube"].includes(platform)) settings.open = true; draftChanged(); }
      return;
    }
    if (model.maintenance.draining) { toast("正在安全维护，暂不接受账号操作。", true); return; }
    if (action === "chrome") return connectChrome(platform);
    if (model.accountBusy.has(platform)) return;
    model.accountBusy.add(platform);
    updatePlatformState();
    try {
      const data = await api(`/api/accounts/${encodeURIComponent(platform)}/${action}`, { method: "POST", body: {} });
      if (!data.job?.id) throw new Error("服务未返回账号任务编号，请刷新记录确认。");
      upsertJob(data.job);
      renderJobs();
      toast(action === "login" ? (platform === "bilibili" ? "正在生成 B站二维码，请在此页面用哔哩哔哩 App 扫码。" : `${platformName(platform)} 登录任务已启动，请在弹出的平台窗口完成登录。`) : `${platformName(platform)} 账号检查已开始。`);
      await showJob(data.job.id);
    } catch (error) { toast(errorMessage(error), true); }
    finally { model.accountBusy.delete(platform); updatePlatformState(); }
  }

  function renderBrowserPair() {
    const pair = model.pairing;
    if (!pair) return;
    $("#browser-pair-title").textContent = `连接 ${platformName(pair.platform)} 的现有 Chrome 账号`;
    const pending = pair.status === "pending";
    $("#browser-pair-code").textContent = pending ? pair.code : "";
    $("#browser-pair-code-wrap").hidden = !pending;
    $("#browser-pair-copy").disabled = !pending;
    $("#browser-pair-job").hidden = !pair.jobId;
    const platform = model.platforms.find((item) => item.id === pair.platform);
    const job = model.jobs.find((item) => item.id === pair.jobId);
    const result = job?.results?.find((item) => item.platform === pair.platform);
    const verificationFailed = pair.status === "imported" && job && !runningStatuses.has(job.status) && ["failed", "error", "needs_login", "needs_attention"].includes(result?.status || job.status);
    const verificationPassed = pair.status === "imported" && job && !runningStatuses.has(job.status) && (result?.status || job.status) === "verified";
    const verificationMessage = verificationFailed ? `在线核验未通过。${platform?.account_message || result?.message || "请查看核验任务的详细日志。"} 请先查看平台卡片中的原因与限制。` : verificationPassed ? "会话已通过在线核验。发布流程及结果仍需单独确认。" : "";
    $("#browser-pair-status").textContent = verificationMessage || pair.message || ({ starting: "正在生成连接码…", pending: "等待 Chrome 扩展同步。连接码 10 分钟内有效，关闭此窗口会取消尚未完成的连接。", imported: "已从 Chrome 同步，正在在线核验。同步完成不代表账号已验证，请查看核验结果。", expired: "连接码已过期。请关闭此窗口，再次点击「连接现有 Chrome」。", failed: "连接失败，请查看错误原因和平台卡片中的限制说明。" }[pair.status] || "连接状态待确认");
    $("#browser-pair-status").classList.toggle("inline-error", verificationFailed || ["failed", "expired"].includes(pair.status));
  }

  async function cancelBrowserCode(code) {
    try { await api("/api/browser/cancel", { method: "POST", body: { code } }); }
    catch (error) { toast(`取消连接请求未确认；连接码会在 10 分钟内失效。${errorMessage(error)}`, true); }
  }

  async function connectChrome(platform) {
    if (model.maintenance.draining || !supportsChrome(platform) || !model.token || model.pairing || model.accountBusy.has(platform)) return;
    const pair = { platform, status: "starting", code: "", polling: false, jobId: null };
    model.pairing = pair;
    renderBrowserPair();
    $("#browser-pair-dialog").showModal();
    try {
      const data = await api("/api/browser/pair", { method: "POST", body: { platform } });
      if (typeof data.code !== "string" || !/^[a-f0-9]{64}$/.test(data.code)) throw new Error("服务未返回有效连接码，请重试。");
      if (model.pairing !== pair) { await cancelBrowserCode(data.code); return; }
      pair.code = data.code;
      pair.status = "pending";
      renderBrowserPair();
    } catch (error) {
      if (model.pairing === pair) { pair.status = "failed"; pair.message = errorMessage(error); renderBrowserPair(); }
    }
  }

  async function pollBrowserPair() {
    const pair = model.pairing;
    if (pair?.status === "imported" && pair.refreshNeeded && !model.refreshing) { pair.refreshNeeded = false; await refreshState({ quiet: true }); return; }
    if (pair?.status === "imported") renderBrowserPair();
    if (!pair || pair.status !== "pending" || pair.polling || !$("#browser-pair-dialog").open) return;
    pair.polling = true;
    try {
      const data = await api("/api/browser/status", { method: "POST", body: { code: pair.code } });
      if (model.pairing !== pair) return;
      pair.message = null;
      if (["imported", "expired", "failed"].includes(data.status)) {
        pair.status = data.status;
        pair.jobId = data.job_id || null;
        pair.code = "";
        if (data.status === "failed") pair.message = data.message || "连接失败，请查看平台卡片中的原因与限制。";
      }
      renderBrowserPair();
      if (pair.status === "imported") {
        pair.refreshNeeded = model.refreshing;
        if (!pair.refreshNeeded) await refreshState({ quiet: true });
      }
    } catch (error) {
      if (model.pairing === pair) { pair.message = `暂时无法读取同步结果，将继续重试。${errorMessage(error)}`; renderBrowserPair(); }
    } finally { pair.polling = false; }
  }

  async function closeBrowserPair() {
    const pair = model.pairing;
    model.pairing = null;
    $("#browser-pair-code").textContent = "";
    $("#browser-pair-dialog").close();
    if (pair?.status === "pending" && pair.code) await cancelBrowserCode(pair.code);
  }

  async function copyBrowserCode() {
    const pair = model.pairing;
    if (!pair || pair.status !== "pending" || !pair.code) return;
    try { await navigator.clipboard.writeText(pair.code); toast("连接码已复制，请在已登录平台页的 Chrome 扩展中粘贴。"); }
    catch { toast("暂时无法自动复制，请选中窗口内的连接码手动复制。", true); }
  }

  function upsertJob(job) {
    const index = model.jobs.findIndex((item) => item.id === job.id);
    if (index < 0) model.jobs.unshift(job);
    else model.jobs[index] = { ...model.jobs[index], ...job };
  }

  function jobTitle(job) {
    if (["login", "account_login"].includes(job.kind)) return `${platformName(job.platform || job.platform_id || job.results?.[0]?.platform)} · 账号登录`;
    if (["check", "account_check"].includes(job.kind)) return `${platformName(job.platform || job.platform_id || job.results?.[0]?.platform)} · 账号检查`;
    return job.title || job.payload?.title || job.request?.title || "视频发布任务";
  }

  function resultProgress(result) {
    let label = ({ preparation: "准备文件与登录会话", uploading: "向平台传输视频", processing: "平台正在处理视频", metadata: "填写标题、简介与声明", cover: "设置封面", ready_to_submit: "材料已填好，待你检查并发布", prepared: "填写完成，等待你在浏览器发布", submitting: "正在点击最终提交", submitted: "已提交，等待平台确认" })[result.phase];
    if (['uploading','processing'].includes(result.phase) && typeof result.percent === 'number' && Number.isFinite(result.percent) && result.percent >= 0 && result.percent <= 100) label += ` · ${result.percent}%`;
    if (label && result.status === 'running' && result.phase_started_at) {
      const elapsed = Math.floor((Date.now() - Date.parse(result.phase_started_at)) / 1000);
      if (Number.isFinite(elapsed) && elapsed >= 0) label += ` · 已等待 ${elapsed >= 60 ? `${Math.floor(elapsed / 60)}分${elapsed % 60}秒` : `${elapsed}秒`}`;
    }
    return label ? `${["failed", "needs_attention", "interrupted", "cancelled"].includes(result.status) ? "最后阶段" : "进展"}：${label}` : ["failed", "needs_attention", "interrupted"].includes(result.status) ? "最后阶段：暂无记录，请查看任务日志" : "";
  }

  function recoverableJob(job) { return ["publish", "upload"].includes(job.kind); }

  function recoveryButton(job, onlyPlatform = null) {
    return `<button type="button" class="button button-small button-secondary" data-restore-job="${escape(job.id)}"${onlyPlatform ? ` data-retry-platform="${escape(onlyPlatform)}"` : ""} ${model.restoring || model.uploading.size || model.submitting ? "disabled" : ""}>${onlyPlatform ? "仅重试该平台" : "恢复材料"}</button>`;
  }

  function resolutionActions(job, result) {
    if (!["needs_attention", "prepared"].includes(result.status) || runningStatuses.has(job.status)) return "";
    const prepared = result.status === "prepared";
    const disabled = model.resolving.has(`${job.id}:${result.platform}`) || model.coverResuming.has(job.id) || !model.token;
    return `<div class="manual-resolution"><p>${prepared ? "完成检查后，记录你的实际操作：" : "核对平台创作后台后，手动记录结果："}</p><div><button type="button" class="button button-small button-secondary" data-resolve-job="${escape(job.id)}" data-resolve-platform="${escape(result.platform)}" data-resolution="not_submitted" ${disabled ? "disabled" : ""}>${prepared ? "未发布，重新准备" : "我已核对：没有提交"}</button><button type="button" class="button button-small button-ghost" data-resolve-job="${escape(job.id)}" data-resolve-platform="${escape(result.platform)}" data-resolution="submitted" ${disabled ? "disabled" : ""}>${prepared ? "我已发布" : "我已找到稿件"}</button></div><p>仅更新本地记录；重新准备还需你恢复材料并开始任务，不会自动重跑。</p></div>`;
  }

  function canResumeCover(job, result) {
    return job?.browser_mode !== "chrome" && job?.mode === "prepare" && !runningStatuses.has(job.status) && result?.platform === "xiaohongshu" && result.status === "needs_attention" && result.phase === "cover";
  }

  function resumeCoverAction(job, result) {
    if (!canResumeCover(job, result)) return "";
    const busy = model.coverResuming.has(job.id);
    const disabled = busy || model.resolving.has(`${job.id}:xiaohongshu`) || !model.token || model.maintenance.draining;
    return `<div class="resume-cover-action"><button type="button" class="button button-small button-secondary" data-resume-cover-job="${escape(job.id)}" ${disabled ? "disabled" : ""}>${busy ? "正在接续封面…" : "继续设置封面"}</button><p>复用保留的原编辑页，原视频和文案必须仍在。只接续封面，不重传视频、不点击最终发布。</p></div>`;
  }

  async function resumeCover(id) {
    const job = model.jobs.find((item) => item.id === id);
    const result = job?.results?.find((item) => item.platform === "xiaohongshu");
    if (!canResumeCover(job, result) || model.coverResuming.has(id) || model.resolving.has(`${id}:xiaohongshu`) || !model.token || model.maintenance.draining) return;
    if (!window.confirm("继续为这个任务设置小红书封面？\n\n将复用保留的原编辑页，原视频、标题和正文必须仍在；请先确认没有关闭或改用其他稿件页面。\n\n只接续同一任务的封面步骤，不重传视频、不重新填写文案、不运行其他平台，也不会点击最终发布。")) return;
    model.coverResuming.add(id);
    if (model.detailId === id) renderJobDetails(job);
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/resume-cover`, { method: "POST", body: { platform: "xiaohongshu" } });
      if (data.job?.id !== id) throw new Error("服务未返回原任务的最新状态，请刷新详情确认；不要重新创建任务。");
      upsertJob(data.job);
      renderJobs();
      if (model.detailId === id) renderJobDetails(model.jobs.find((item) => item.id === id));
      toast("已请求接续原任务的小红书封面步骤，不会重传视频或运行其他平台。完成后仍由你检查并发布。");
      await refreshState({ quiet: true });
    } catch (error) { toast(`封面续接未确认，请先刷新任务状态。${errorMessage(error)}`, true); }
    finally {
      model.coverResuming.delete(id);
      if (model.detailId === id) renderJobDetails(model.jobs.find((item) => item.id === id));
    }
  }

  function resultRecovery(job, result) {
    if (!recoverableJob(job)) return "";
    if (result.status === "prepared") return `<p class="manual-publish-notice">待你发布：请切回已经保留的专用浏览器窗口，检查内容、封面和声明，最后亲自点击发布。</p>${resolutionActions(job, result)}`;
    if (["needs_attention", "interrupted"].includes(result.status)) {
      return `<p class="result-attention">待核验，不自动重试。下一步：先到平台创作后台检查稿件，再记录核验结果。</p>${resumeCoverAction(job, result)}${resolutionActions(job, result)}`;
    }
    if (canRetry(result) && supportsPrepare(result.platform)) return `<div class="result-recovery">${recoveryButton(job, result.platform)}<span>下一步：恢复材料，检查失败原因后重新上传填写，最后由你发布。</span></div>`;
    if (canRetry(result)) return '<p class="result-attention">下一步：可先恢复材料；该平台目前不支持上传并填写，请查看平台卡说明。</p>';
    if (result.status === "failed") return '<p class="result-attention">尚未确认可以安全重试。下一步：先核对平台稿件和任务日志，避免重复提交。</p>';
    return "";
  }

  async function resolveResult(id, platform, resolution) {
    const job = model.jobs.find((item) => item.id === id);
    const key = `${id}:${platform}`;
    const result = job?.results?.find((item) => item.platform === platform);
    if (!job || runningStatuses.has(job.status) || !["needs_attention", "prepared"].includes(result?.status) || !["not_submitted", "submitted"].includes(resolution) || model.resolving.has(key) || model.coverResuming.has(id)) return;
    const prepared = result.status === "prepared";
    const question = resolution === "not_submitted" ? `请先核对 ${platformName(platform)} 创作后台，确认平台没有这条视频的稿件（含已提交、待审核和已发布），并且不再使用原来保留的窗口继续发布。\n\n确认记录「${prepared ? "未发布，重新准备" : "没有提交"}」？此操作只解除该平台在本地记录中的重试锁，不会上传或发布，也不会自动重跑。之后仍需恢复材料并点击「上传并填写」，最终在浏览器自行发布。` : `你已在 ${platformName(platform)} ${prepared ? "完成这条视频的最终发布，并能在创作后台找到对应稿件" : "创作后台找到这条任务对应的稿件"}？\n\n确认后只将本地任务记为「用户已核验提交」，不会上传、再次提交，也不代表平台已经审核通过。`;
    if (!window.confirm(question)) return;
    model.resolving.add(key);
    if (model.detailId === id) renderJobDetails(job);
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/resolve`, { method: "POST", body: { platform, resolution, confirmed: true } });
      if (!data.job?.id) throw new Error("服务未返回核验后的任务记录，请刷新详情确认。");
      upsertJob(data.job);
      renderJobs();
      if (model.detailId === id) renderJobDetails(model.jobs.find((item) => item.id === id));
      toast(resolution === "not_submitted" ? "已记录为没有提交。可恢复材料后手动重试，尚未发起发布。" : "已记录为用户核验已提交，请以平台稿件状态为准。");
    } catch (error) { toast(errorMessage(error), true); }
    finally { model.resolving.delete(key); if (model.detailId === id) renderJobDetails(model.jobs.find((item) => item.id === id)); }
  }

  function renderJobs() {
    const isAccount = job => ["login","check","account_login","account_check"].includes(job.kind);
    const attention = new Set(["failed","error","partial","needs_attention","interrupted","cancelled"]);
    const publishJobs = model.jobs.filter(job => !isAccount(job));
    $("#history-count").textContent = publishJobs.length;
    const jobs = model.jobs.filter(job => model.historyFilter === "accounts" ? isAccount(job) : model.historyFilter === "prepared" ? !isAccount(job) && job.results?.some(r => r.status === "prepared") : model.historyFilter === "attention" ? !isAccount(job) && attention.has(job.status) : !isAccount(job));
    for (const button of $$("[data-history-filter]")) { const active = button.dataset.historyFilter === model.historyFilter; button.classList.toggle("active", active); button.setAttribute("aria-pressed",String(active)); }
    if (!jobs.length) { $("#job-list").innerHTML = '<div class="empty-state"><span aria-hidden="true">↗</span><strong>这里还没有任务</strong><p>准备好一条视频后，进度和结果会显示在这里。</p><a href="#compose" class="button button-secondary">去准备视频</a></div>'; return; }
    $("#job-list").innerHTML = jobs.map(job => `<article class="job-row"><span class="job-type-icon" aria-hidden="true">${isAccount(job) ? "◉" : "↗"}</span><div class="job-main"><div class="job-title"><strong>${escape(jobTitle(job))}</strong>${pill(job.status)}</div><div class="job-meta"><span>${escape(formatDate(job.created_at))}</span><span>${escape((job.results || []).map(r => platformName(r.platform)).join(" · "))}</span></div>${job.results?.length ? `<div class="result-mini">${job.results.map(result => `<span>${escape(platformName(result.platform))} · ${escape(resultProgress(result) || statusInfo(result.status)[0])}</span>`).join("")}</div>` : ""}${job.results?.some(result => result.status === "prepared") ? '<p class="job-next-step">材料已填好，请到保留的平台窗口确认发布。</p>' : ""}${["needs_attention","interrupted"].includes(job.status) ? '<p class="result-attention">需要核对平台状态，请查看详情。</p>' : ""}</div><div class="job-row-actions">${recoverableJob(job) ? recoveryButton(job) : ""}<button type="button" class="button button-small button-secondary" data-job="${escape(job.id)}">查看详情</button></div></article>`).join("");
  }

  function renderJobDetails(job) {
    $("#dialog-title").textContent = jobTitle(job);
    const logElement = $(".job-logs");
    const wasNearBottom = !logElement || logElement.scrollHeight - logElement.scrollTop - logElement.clientHeight < 60;
    $("#dialog-content").innerHTML = `<p class="detail-meta">${pill(job.status)}　${escape(formatDate(job.created_at))}<br>任务编号：${escape(job.id)}</p>${recoverableJob(job) ? `<div class="draft-recovery-actions">${recoveryButton(job)}<p>恢复标题、封面、设置和本地视频，无需重新上传已有文件。${["needs_attention", "interrupted"].includes(job.status) ? "待核验，不自动重试；请先确认平台实际结果。" : "恢复材料不会发起发布。"}</p></div>` : ""}<div class="detail-results">${job.results?.length ? job.results.map((result) => { const url = safeLink(result.url); return `<div class="detail-result"><div><strong>${escape(platformName(result.platform))}</strong>${pill(result.status)}</div>${resultProgress(result) ? `<p class="result-progress">${escape(resultProgress(result))}</p>` : ""}<p>${escape(result.message || "尚无详细说明")}</p>${result.cover_status ? `<p>封面：${escape(result.cover_status)}</p>` : ""}${url ? `<a class="result-link" href="${escape(url)}" target="_blank" rel="noopener noreferrer">${result.status === "prepared" ? "平台参考链接（不会恢复原编辑窗口）" : "打开平台结果"} ↗</a>` : ""}${resultRecovery(job, result)}</div>`; }).join("") : '<p class="field-help">暂时没有平台结果，执行日志会随任务更新。</p>'}</div><h3 class="log-heading">执行日志 ${runningStatuses.has(job.status) ? "· 每 3 秒自动更新" : ""}</h3><pre class="job-logs"></pre>`;
    if (["queued", "running"].includes(job.status)) {
      const action = document.createElement("div");
      action.className = "dialog-task-actions";
      action.innerHTML = `<button type="button" class="button button-small cancel-job" data-cancel-job="${escape(job.id)}" ${model.cancelRequested.has(job.id) ? "disabled" : ""}>${model.cancelRequested.has(job.id) ? "已请求停止" : "停止此任务"}</button><p>已提交到平台的内容不会被撤回；停止后请核验平台结果，避免重复发布。</p>`;
      $(".detail-meta").after(action);
    }
    if (job.kind === "login" && job.status === "running" && job.results?.some((result) => result.platform === "bilibili")) {
      const qr = document.createElement("div");
      qr.className = "qr-login";
      qr.innerHTML = '<strong>B站扫码登录</strong><p>正在等待二维码，请保持任务运行。</p><img alt="B站登录二维码" hidden>';
      $(".detail-results").before(qr);
      const image = $("img", qr);
      image.addEventListener("load", () => { image.hidden = false; $("p", qr).textContent = "请用哔哩哔哩 App 扫码，并在手机上确认登录。"; });
      image.addEventListener("error", () => { image.hidden = true; $("p", qr).textContent = "二维码尚未生成或已过期，正在自动检查…"; });
      image.src = apiURL(`/api/accounts/bilibili/qr?ts=${Date.now()}`);
    }
    $(".job-logs").textContent = Array.isArray(job.logs) && job.logs.length ? job.logs.map((line) => typeof line === "string" ? line : JSON.stringify(line)).join("\n") : "暂时没有日志。";
    if (wasNearBottom) $(".job-logs").scrollTop = $(".job-logs").scrollHeight;
  }

  async function showJob(id) {
    model.detailId = id;
    $("#dialog-title").textContent = "任务详情";
    $("#dialog-content").innerHTML = '<p class="detail-loading">正在读取日志…</p>';
    if (!$("#job-dialog").open) $("#job-dialog").showModal();
    try { const data = await api(`/api/jobs/${encodeURIComponent(id)}`); if (model.detailId !== id) return; if (!data.job) throw new Error("没有找到这个任务。"); upsertJob(data.job); renderJobDetails(data.job); renderJobs(); }
    catch (error) { if (model.detailId === id) $("#dialog-content").innerHTML = `<p class="inline-error">${escape(errorMessage(error))}</p>`; }
  }

  async function cancelJob(id) {
    if (model.cancelRequested.has(id)) return;
    if (!window.confirm("确认停止此任务？\n\n已经提交给平台的内容不会撤回。运行中的平台结果可能需要手动核验，请先查看平台再决定是否重新发布。")) return;
    model.cancelRequested.add(id);
    const button = $(`[data-cancel-job="${CSS.escape(id)}"]`);
    if (button) { button.disabled = true; button.textContent = "正在请求停止…"; }
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} });
      if (data.job) { upsertJob(data.job); renderJobs(); if (model.detailId === id) renderJobDetails(data.job); }
      toast("已请求停止。已提交内容不会撤回，请核验平台实际结果。");
    } catch (error) { model.cancelRequested.delete(id); if (button) { button.disabled = false; button.textContent = "停止此任务"; } toast(errorMessage(error), true); }
  }

  async function refreshState({ quiet = false } = {}) {
    if (model.refreshing) return;
    model.refreshing = true;
    $("#refresh-state").disabled = true;
    $("#refresh-jobs").disabled = true;
    const jobsAtRequest = new Map(model.jobs.map((job) => [job.id, job]));
    try {
      const data = await api("/api/state");
      model.token = data.token;
      model.platforms = data.platforms || [];
      model.runtime = data.runtime || { ready: false };
      if (typeof data.submission_mode === "string") model.submissionMode = data.submission_mode;
      if (Array.isArray(data.prepare_platforms)) model.preparePlatforms = data.prepare_platforms.filter((id) => typeof id === "string" && /^[a-z][a-z0-9_-]*$/.test(id));
      if (data.maintenance && typeof data.maintenance === "object") model.maintenance = { ...model.maintenance, ...data.maintenance };
      model.browserBridge = data.browser_bridge || null;
      model.nativeChrome = data.native_chrome || null;
      const chromeStatus = $('#native-chrome-status');
      if (chromeStatus) chromeStatus.textContent = model.nativeChrome?.connected ? '普通 Chrome 已连接 · 使用当前浏览器登录' : '普通 Chrome 尚未连接 · 先连接扩展，再上传';
      if (Array.isArray(data.jobs)) {
        const previous = new Map(model.jobs.map((job) => [job.id, job]));
        const changed = (job) => previous.has(job.id) && previous.get(job.id) !== jobsAtRequest.get(job.id);
        const incoming = new Set(data.jobs.map((job) => job.id));
        model.jobs = [...model.jobs.filter((job) => !incoming.has(job.id) && changed(job)), ...data.jobs.map((job) => changed(job) ? previous.get(job.id) : ({ ...previous.get(job.id), ...job }))];
      }
      renderPlatforms(); renderRuntime(); updatePlatformState(); renderJobs();
      if (model.pairing) renderBrowserPair();
      await restoreSessionDraft();
    } catch (error) { model.token = null; model.runtime = { ready: false }; renderRuntime(errorMessage(error)); updatePlatformState(); if (!quiet) toast(errorMessage(error), true); }
    finally { model.refreshing = false; $("#refresh-state").disabled = false; $("#refresh-jobs").disabled = false; }
  }

  let polling = false;
  async function poll() {
    if (polling || document.hidden) return;
    const running = model.jobs.some((job) => runningStatuses.has(job.status));
    if (!running && !model.detailId && !model.maintenance.draining) return;
    polling = true;
    try {
      if (running || model.maintenance.draining) await refreshState({ quiet: true });
      const id = model.detailId;
      if (id && $("#job-dialog").open) { const data = await api(`/api/jobs/${encodeURIComponent(id)}`); if (data.job && model.detailId === id) { upsertJob(data.job); renderJobDetails(data.job); renderJobs(); } }
    } catch (error) {
      if (model.detailId && $("#job-dialog").open) {
        let warning = $("#dialog-refresh-error");
        if (!warning) { warning = document.createElement("p"); warning.id = "dialog-refresh-error"; warning.className = "inline-error"; warning.setAttribute("role", "status"); $("#dialog-content").prepend(warning); }
        warning.textContent = `日志更新失败，以下是上次读取的内容：${errorMessage(error)}`;
      }
    }
    finally { polling = false; }
  }

  renderPlatforms();
  $("#publish-form").addEventListener("submit", (event) => event.preventDefault());
  $("#publish-button").addEventListener("click", publish);
  $("#validate-button").addEventListener("click", validate);
  $("#refresh-state").addEventListener("click", () => refreshState());
  let nativeExtensionAvailable = false;
  const nativePairRequests = new Map();
  window.addEventListener('message', event => {
    if (event.source !== window || event.origin !== window.location.origin || event.data?.source !== 'publisher-extension') return;
    if (event.data.type === 'ready') nativeExtensionAvailable = true;
    if (event.data.type === 'connection' && model.nativeChrome?.connected !== event.data.connected) refreshState({quiet:true});
    if (event.data.type === 'paired') {
      const pending = nativePairRequests.get(event.data.requestId);
      if (pending) { nativePairRequests.delete(event.data.requestId); pending(event.data.ok === true); }
    }
  });
  window.postMessage?.({source:'publisher-page',type:'hello'},window.location.origin);
  $('#native-chrome-connect')?.addEventListener('click', async () => {
    if (model.nativeChrome?.connected) { toast('普通 Chrome 已连接，可以直接开始准备视频。'); return; }
    // Content scripts and deferred page scripts can initialize in either order.
    // Re-query presence from this click rather than relying on one startup event.
    if (!nativeExtensionAvailable) {
      window.postMessage?.({source:'publisher-page',type:'hello'},window.location.origin);
      await new Promise(resolve => setTimeout(resolve,700));
    }
    if (!nativeExtensionAvailable) {
      $('#native-chrome-help').hidden = false;
      toast('首次使用需要安装一次 Chrome 连接组件；安装后刷新页面即可。',true); return;
    }
    const button = $('#native-chrome-connect'); button.disabled = true;
    try {
      const data = await api('/api/chrome/code', {method:'POST',body:{}});
      const requestId = Date.now().toString(36) + Math.random().toString(36).slice(2);
      const result = new Promise(resolve => {
        nativePairRequests.set(requestId,resolve);
        setTimeout(() => { if (nativePairRequests.delete(requestId)) resolve(false); },15000);
      });
      window.postMessage({source:'publisher-page',type:'pair',requestId,code:data.code},window.location.origin);
      if (!await result) throw new Error('Chrome 连接尚未完成，请重新加载扩展后再试。');
      $('#native-chrome-help').hidden = true;
      toast('配对完成，正在连接普通 Chrome。');
      await refreshState({quiet:true});
    } catch(error) { toast(errorMessage(error),true); }
    finally { button.disabled = false; }
  });
  $('#native-chrome-test')?.addEventListener('click', async () => {
    const button=$('#native-chrome-test');button.disabled=true;
    try {
      const result=await api('/api/chrome/test-open',{method:'POST',body:{}});
      toast(`已请求在普通 Chrome 打开 ${result.opened.length} 个平台页面；未上传或发布。`);
    } catch(error) {toast(errorMessage(error),true);}
    finally {button.disabled=false;}
  });
  $("#refresh-jobs").addEventListener("click", () => refreshState());
  $("#refresh-accounts").addEventListener("click", () => refreshState());
  $("#clear-draft").addEventListener("click", clearDraft);
  $(".local-video-preview").addEventListener("error", videoPreviewUnavailable);
  $(".local-video-preview").addEventListener("loadedmetadata", () => { $(".video-preview-notice").hidden = true; });
  $("#save-template").addEventListener("click", saveTemplate);
  $("#apply-template").addEventListener("click", applyTemplate);
  $("#delete-template").addEventListener("click", deleteTemplate);
  $("#template-select").addEventListener("change", updateActions);
  $("#close-asset-picker").addEventListener("click", closeAssetPicker);
  $("#asset-picker").addEventListener("cancel", (event) => { event.preventDefault(); closeAssetPicker(); });
  $("#asset-picker").addEventListener("close", () => { model.assetPicker = null; });
  $("#refresh-assets").addEventListener("click", () => { if (model.assetPicker && !model.assetPicker.selecting) openRecentAssets(model.assetPicker.slot); });
  $("#title").addEventListener("input", () => { $("#title-count").textContent = `${Array.from($("#title").value).length} 字`; });
  $("#publish-form").addEventListener("input", (event) => { if (event.target.type !== "file") draftChanged(); });
  $("#publish-form").addEventListener("change", (event) => { if (event.target.name === "platform" && event.target.checked && ["bilibili", "youtube", "weibo", "hupu", "tencent"].includes(event.target.value)) event.target.closest(".platform-card").querySelector("details").open = true; if (event.target.type !== "file") draftChanged(); });
  document.addEventListener("click", (event) => {
    const nav = event.target.closest('a[href^="#"]');
    if (nav && ["#compose","#accounts","#history"].includes(nav.getAttribute("href"))) { event.preventDefault(); showView(nav.getAttribute("href").slice(1)); return; }
    const filter = event.target.closest("[data-history-filter]"); if (filter) { model.historyFilter = filter.dataset.historyFilter; renderJobs(); }
    const recent = event.target.closest("[data-recent-slot]"); if (recent) openRecentAssets(recent.dataset.recentSlot);
    const asset = event.target.closest("[data-use-asset]"); if (asset) useRecentAsset(asset.dataset.useAsset);
    const focus = event.target.closest("[data-focus-field]"); if (focus) focusField(focus.dataset.focusField);
    const choose = event.target.closest(".choose-file"); if (choose) $(`#${choose.dataset.slot}-file`).click();
    const remove = event.target.closest(".remove-file"); if (remove) removeAsset(remove.dataset.slot);
    const account = event.target.closest("[data-account-action]"); if (account) accountAction(account.dataset.platform, account.dataset.accountAction);
    const log = event.target.closest("[data-account-log]"); if (log) { const platform = model.platforms.find((item) => item.id === log.dataset.accountLog); const id = latestAccountJob(log.dataset.accountLog)?.id || platform?.login_job_id; if (id) showJob(id); }
    const job = event.target.closest("[data-job]"); if (job) showJob(job.dataset.job);
    const cancel = event.target.closest("[data-cancel-job]"); if (cancel) cancelJob(cancel.dataset.cancelJob);
    const restore = event.target.closest("[data-restore-job]"); if (restore) restoreJobDraft(restore.dataset.restoreJob, restore.dataset.retryPlatform || null);
    const resolve = event.target.closest("[data-resolve-job]"); if (resolve) resolveResult(resolve.dataset.resolveJob, resolve.dataset.resolvePlatform, resolve.dataset.resolution);
    const resume = event.target.closest("[data-resume-cover-job]"); if (resume) resumeCover(resume.dataset.resumeCoverJob);
  });
  for (const zone of $$(".upload-zone")) {
    const slot = zone.dataset.slot;
    $("input", zone).addEventListener("change", (event) => uploadFile(slot, event.target.files[0]));
    for (const type of ["dragenter", "dragover"]) zone.addEventListener(type, (event) => { event.preventDefault(); zone.classList.add("dragging"); });
    zone.addEventListener("dragleave", (event) => { if (!zone.contains(event.relatedTarget)) zone.classList.remove("dragging"); });
    zone.addEventListener("drop", (event) => { event.preventDefault(); zone.classList.remove("dragging"); if (event.dataTransfer.files.length > 1) toast("每个位置一次选择一个文件。", true); else uploadFile(slot, event.dataTransfer.files[0]); });
  }
  for (const type of ["dragover", "drop"]) window.addEventListener(type, (event) => { if (event.dataTransfer?.types.includes("Files")) event.preventDefault(); });
  $("#close-dialog").addEventListener("click", () => $("#job-dialog").close());
  $("#browser-pair-close").addEventListener("click", closeBrowserPair);
  $("#browser-pair-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeBrowserPair(); });
  $("#browser-pair-dialog").addEventListener("close", () => { if (model.pairing) closeBrowserPair(); });
  $("#browser-pair-copy").addEventListener("click", copyBrowserCode);
  $("#browser-pair-job").addEventListener("click", async () => { const id = model.pairing?.jobId; await closeBrowserPair(); if (id) showJob(id); });
  $("#job-dialog").addEventListener("close", () => { model.detailId = null; });
  $("#job-dialog").addEventListener("click", (event) => { if (event.target === $("#job-dialog")) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.target.close(); } });
  window.addEventListener("hashchange", () => showView((window.location.hash || "#compose").slice(1), {updateHash:false}));
  window.addEventListener("popstate", () => showView((window.location.hash || "#compose").slice(1), {updateHash:false}));
  showView((window.location.hash || "#compose").slice(1), {updateHash:false});
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  updateActions();
  model.templates = readTemplates();
  renderTemplates();
  model.sessionDraft = readSessionDraft();
  refreshState({ quiet: true });
  setInterval(poll, 3000);
  setInterval(pollBrowserPair, 3000);
})();
