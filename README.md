# 视频发布台 · Video Publisher

本地运行的视频分发辅助工具：准备一次视频、封面与文案，在网页中选择平台，由 Python 服务与 Chrome 扩展完成上传和填写，最后由本人检查并点击发布。

> 当前是实验性、人工确认版本。平台页面变化可能使适配失效；不承诺全平台自动发布。

## 功能与状态

- 视频和封面复用、常用文案模板、平台独立字段、材料检查。
- 本地任务队列、进度显示、失败阶段提示和重复任务拦截。
- 普通 Chrome 扩展连接，以及部分平台的专用浏览器流程。
- 所有账号状态与素材留在自己的电脑；上传时由本机发送到所选平台。

| 平台 | 已有实现与验证边界 |
| --- | --- |
| 抖音 | 普通 Chrome 上传、文案和封面准备有 macOS 小视频实测记录，未代用户点击发布 |
| B站 | 网页上传、文案和封面准备有 macOS 小视频实测记录，未代用户点击投稿 |
| 小红书 | 上传和文案流程有实测记录；封面与最终交接仍需进一步验收 |
| YouTube | 已实现人工接手流程，真实完整流程待验收 |
| 微信视频号 | 实验性适配，生产入口未开放 |
| 其他上游平台 | 保留代码不代表当前产品开放支持 |

Windows 安装入口尚未实机验证。大文件、不同账号和平台改版均需要独立验证。

## 快速开始

需要 Python **3.11 或 3.12**，普通 Chrome 路径需要 **Chrome 125+**。测试和 Chrome 中继需要 Node.js（建议 22+）。

```bash
git clone https://github.com/ohohohoimSuu/video-publisher.git
cd video-publisher
bash setup.sh --with-browser
.venv/bin/python server.py --port 8765
```

打开 http://127.0.0.1:8765 。macOS 也可以双击 `启动发布台.command`；首次安装需要联网下载依赖和浏览器。关闭启动终端将停止服务。

### 安装 Chrome 扩展

1. 在 Chrome 打开 `chrome://extensions` 并启用开发者模式。
2. 点击“加载已解压的扩展程序”，选择本项目 `chrome-extension` 文件夹。
3. 打开本地发布台，按照页面连接提示配对。
4. 使用本人有权操作的账号，选择视频、封面，填写文案和平台声明。
5. 检查材料后点击“上传并填写”。完成后到平台编辑页核对，再亲自点击最终发布。

扩展使用 `debugger` 权限执行所选平台页面的上传与填写，Chrome 可能显示调试提示。连接说明以本地 `/browser-setup` 页面为准。

## 数据与配置

- 服务默认仅监听 `127.0.0.1:8765`。
- `data/` 存放本机运行数据，不应提交到 GitHub。
- `configure.py` 根据 `conf.example.py` 创建本地配置，不覆盖已有配置。
- 仓库不附带账号、Cookie、浏览器 profile、个人视频或已部署站点配置。
- 默认使用本地网页。若自行托管前端，需按 `web_access.py` 的校验规则显式配置可信来源，并调整扩展匹配来源；不要直接把本地服务暴露到公网。
- 不提供定时发布、同平台多账号批量发布、审核结果自动回查。

## 开发与验证

```bash
bash verify.sh
# 另测本地 HTTP 与安全退出
bash verify.sh --http
# 全部 Node 测试
node --test tests/test_*.cjs
```

测试主要使用模拟页面和本地数据，通过测试不代表平台真实发布成功。真实账号验证由使用者主动执行；不要在 Issue 中附带 Cookie、令牌、账号快照或私人素材。

## 结构

- `server.py`：本地服务、素材、队列与 API。
- `adapter.py`、`native_chrome.py`：平台准备与普通 Chrome 连接。
- `static/`：网页界面。
- `chrome-extension/`：Chrome 扩展。
- `vendor/social-auto-upload/`：上游代码及人工接手改动。
- `tests/`：回归测试。

## 开源与贡献

MIT License，允许使用、修改和分发，须保留许可证与版权声明。上游来源和修改范围见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

欢迎提交可复现的 Bug、平台适配修复和安装体验改进。报告问题时写明系统、Python/Chrome 版本、平台与失败阶段，并先清理截图和日志中的个人信息。
