(function () {
  "use strict";
  const input = document.getElementById("code");
  const button = document.getElementById("sync");
  const status = document.getElementById("status");
  const platformLabel = document.getElementById("platform");
  const bridge = LoginBridge.createBridge(chrome, fetch);
  let tab = null;
  let busy = false;

  function show(message, state = "") {
    status.textContent = message;
    status.dataset.state = state;
  }
  function updateButton() {
    button.disabled = busy || !tab || !/^[a-f0-9]{64}$/i.test(input.value.trim());
  }

  input.addEventListener("input", updateButton);
  button.addEventListener("click", function () {
    if (busy || button.disabled) return;
    busy = true;
    input.disabled = true;
    updateButton();
    show("请允许当前平台访问权限；正在连接本地发布台…");
    // Call directly inside the user's click handler, before awaiting any network work.
    const pending = bridge.syncFromClick(tab, input.value);
    pending.then((result) => {
      input.value = "";
      show(`${result.name}：已同步 ${result.cookieCount} 个 Cookie。发布台正在验证，请回到发布台查看结果。`, "success");
    }).catch((error) => {
      show(error instanceof LoginBridge.BridgeError ? error.message : LoginBridge.MESSAGES.unexpected, "error");
    }).finally(() => {
      busy = false;
      input.disabled = false;
      updateButton();
    });
  });

  // Startup reads only the current tab URL; it never reads cookies or page storage.
  chrome.tabs.query({ active: true, currentWindow: true }).then((tabs) => {
    const current = tabs[0];
    const platform = LoginBridge.validateTab(current);
    tab = current;
    platformLabel.textContent = `当前平台：${platform.name}`;
    updateButton();
  }).catch((error) => {
    platformLabel.textContent = "当前标签页不可同步";
    show(error instanceof LoginBridge.BridgeError ? error.message : LoginBridge.MESSAGES.unsupported, "error");
  });
})();
