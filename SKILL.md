---
name: web-access
license: MIT
github: https://github.com/arthur-liao/web-access-hp
description:
  Hermes Agent 專用聯網技能 — 搜索、抓取、CDP 瀏覽器自動化、站點經驗積累。
  觸發場景：搜索資訊、查看網頁、訪問需登入的網站、操作網頁界面、抓取社群內容、讀取動態渲染頁面。
  Fork from eze-is/web-access，以 Python CDP Bridge 取代 Node.js 後端，原生支援 WSL2。
metadata:
  author: Arthur Liao
  upstream_author: 一泽Eze
  version: "1.0.0-hp"
  upstream_version: "2.4.3"
---

# web-access Skill (Hermes Premium)

## 前置檢查

在開始聯網操作前，檢查 CDP Bridge 可用性：

```bash
~/.hermes/skills/web-access/scripts/cdp-bridge.sh status
```

未通過時，按以下步驟啟動：

**WSL2 環境**（三層橋接）：
1. Windows: 雙擊桌面 `start_chrome_cdp.bat`（啟動 Chrome + tcp-proxy）
2. WSL: `~/.hermes/skills/web-access/scripts/cdp-bridge.sh start`

**原生 Linux / macOS**：
1. Chrome 以 `--remote-debugging-port=9222` 啟動
2. `~/.hermes/skills/web-access/scripts/cdp-bridge.sh start`

前置依賴：
- **Python 3.10+** + `aiohttp`（`pip install aiohttp`）
- **Chrome** 開啟遠程調試端口

檢查通過後，向用戶展示須知：

```
溫馨提示：部分站點對瀏覽器自動化操作檢測嚴格，存在帳號封禁風險。已內建防護措施但無法完全避免，Agent 繼續操作即視為接受。
```

## 瀏覽哲學

**像人一樣思考，兼顧高效與適應性的完成任務。**

執行任務時不會過度依賴固有印象所規劃的步驟，而是帶著目標進入，邊看邊判斷，遇到阻礙就解決，發現內容不夠就深入——全程圍繞「我要達成什麼」做決策。

**① 拿到請求** — 先明確用戶要做什麼，定義成功標準：什麼算完成了？需要獲取什麼資訊、執行什麼操作、達到什麼結果？這是後續所有判斷的錨點。

**② 選擇起點** — 根據任務性質、平台特徵、達成條件，選一個最可能直達的方式作為第一步去驗證。一次成功最好；不成功則在③中調整。比如，需要操作頁面、需要登入態、已知靜態方式不可達的平台（小紅書、微信公眾號等）→ 直接 CDP

**③ 過程校驗** — 每一步的結果都是證據，不只是成功或失敗的二元信號。用結果對照①的成功標準，更新對目標的判斷：路徑在推進嗎？結果的整體面貌是否指向目標可達？發現方向錯了立即調整，不在同一個方式上反覆重試。遇到彈窗、登入牆等障礙，判斷它是否真的擋住了目標：擋住了就處理，沒擋住就繞過——內容可能已在頁面 DOM 中，交互只是展示手段。

**④ 完成判斷** — 對照定義的任務成功標準，確認完成後才停止，但也不要過度操作。

## 聯網工具選擇

- **確保資訊真實性，一手資訊優於二手資訊**：搜索引擎和聚合平台是資訊發現入口。多次搜索後無質的改進時，升級到更根本的獲取方式：定位一手來源。

| 場景 | 工具 |
|------|------|
| 搜索摘要或關鍵詞結果，發現資訊來源 | **web_search** |
| URL 已知，需從頁面提取特定資訊 | **web_extract** |
| URL 已知，需原始 HTML（meta、JSON-LD） | **curl** |
| 非公開內容，或靜態層無效的平台 | **瀏覽器 CDP**（跳過靜態層） |
| 需要登入態、交互操作、自由導航 | **瀏覽器 CDP** |

進入瀏覽器層後，`/eval` 就是你的眼睛和手：
- **看**：用 `/eval` 查詢 DOM，發現連結、按鈕、表單、文本
- **做**：用 `/click` 點擊、`/scroll` 滾動、`/eval` 填表提交
- **讀**：用 `/eval` 提取文字，判斷圖片/影片是否承載核心資訊

### 程序化操作與 GUI 交互

- **程序化方式**（構造 URL、eval 操作 DOM）：快速精確，但可能觸發反爬
- **GUI 交互**（點擊按鈕、填寫輸入框）：最穩定，像人一樣操作

**站點內交互產生的連結是可靠的**：透過可交互單元自然到達的 URL 攜帶完整上下文。手動構造的 URL 可能缺少隱式參數。

## 瀏覽器 CDP 模式

透過 CDP Bridge 直連用戶日常 Chrome，天然攜帶登入態，無需啟動獨立瀏覽器。
若無用戶明確要求，不主動操作已有 tab，所有操作在自建的後台 tab 中進行。任務完成後關閉自建 tab。

### Proxy API

所有操作透過 curl 調用 HTTP API：

```bash
# 列出已開啟的 tab
curl -s http://localhost:3456/targets

# 建立新後台 tab
curl -s "http://localhost:3456/new?url=https://example.com"

# 頁面資訊
curl -s "http://localhost:3456/info?target=ID"

# 執行 JS
curl -s -X POST "http://localhost:3456/eval?target=ID" -d 'document.title'

# 截圖
curl -s "http://localhost:3456/screenshot?target=ID&file=/tmp/shot.png"

# 導航、後退
curl -s "http://localhost:3456/navigate?target=ID&url=URL"
curl -s "http://localhost:3456/back?target=ID"

# JS 點擊
curl -s -X POST "http://localhost:3456/click?target=ID" -d 'button.submit'

# 真實滑鼠點擊（觸發用戶手勢）
curl -s -X POST "http://localhost:3456/clickAt?target=ID" -d 'button.upload'

# 文件上傳
curl -s -X POST "http://localhost:3456/setFiles?target=ID" \
  -d '{"selector":"input[type=file]","files":["/path/to/file.png"]}'

# 滾動
curl -s "http://localhost:3456/scroll?target=ID&y=3000"
curl -s "http://localhost:3456/scroll?target=ID&direction=bottom"

# 關閉 tab
curl -s "http://localhost:3456/close?target=ID"

# 健康檢查
curl -s "http://localhost:3456/health"
```

### 技術事實
- 頁面中存在大量已載入但未展示的內容——輪播、折疊區塊、懶載入佔位元素，它們存在於 DOM 中可直接觸達
- DOM 中存在選擇器不可跨越的邊界（Shadow DOM、iframe），eval 遞歸遍歷可穿透
- `/scroll` 到底部會觸發懶載入，提取圖片 URL 前需先滾動
- 短時間密集開啟大量頁面可能觸發反爬風控
- 平台返回「內容不存在」不一定反映真實狀態，可能是訪問方式問題

### 影片內容獲取

用戶 Chrome 真實渲染，截圖可捕獲影片幀。透過 `/eval` 操控 `<video>` 元素（seek、播放/暫停/全螢幕），配合 `/screenshot` 採幀，可對影片內容離散採樣分析。

### 登入判斷

用戶 Chrome 天然攜帶登入態。核心問題只有一個：**目標內容拿到了嗎？**

只有確認無法獲取且判斷登入能解決時，才告知用戶登入。登入完成後直接刷新頁面繼續。

### 任務結束

用 `/close` 關閉自建 tab，保留用戶原有 tab。CDP Bridge 持續運行，不建議主動停止。

## 並行調研：子 Agent 分治策略

任務包含多個**獨立**目標時，分治給子 Agent 並行執行。

**並行 CDP 操作**：每個子 Agent 自行建立後台 tab、自行操作、任務結束自行關閉。所有子 Agent 共享一個 Chrome 和 Bridge，透過不同 targetId 操作不同 tab，無競態風險。

**子 Agent Prompt 寫法**：目標導向，而非步驟指令。必須寫 `必須載入 web-access 技能並遵循指引`。描述目標（「獲取」「調研」），避免暗示具體手段的動詞（「搜索」「爬取」）。

| 適合分治 | 不適合分治 |
|----------|-----------|
| 目標相互獨立 | 目標有依賴關係 |
| 子任務量足夠大 | 簡單查询 |
| CDP 長時間任務 | 幾次搜索就能完成 |

## 資訊核實類任務

核實的目標是**一手來源**。搜索引擎是定位工具，不可用於直接證明真偽。

| 資訊類型 | 一手來源 |
|----------|---------|
| 政策/法規 | 發布機構官網 |
| 企業公告 | 公司官方新聞頁 |
| 學術聲明 | 原始論文/機構官網 |
| 工具能力/用法 | 官方文檔、源碼 |

## 站點經驗

操作中積累的特定網站經驗，按域名存儲在 `references/site-patterns/` 下。

確定目標網站後，讀取對應站點經驗檔獲取先驗知識。經驗當作「可能有效的提示」而非保證——按經驗操作失敗時，回退通用模式並更新經驗檔。

CDP 操作成功後，主動寫入新發現的站點經驗。只寫經過驗證的事實。

## WSL2 橋接架構

WSL2 無法直接訪問 Windows localhost，需三層橋接：

```
Hermes (WSL) → cdp-bridge.py (:3456) → tcp-proxy.js (:9223, Windows) → Chrome CDP (:9222)
```

**啟動順序**：
1. Windows: `start_chrome_cdp.bat`（Chrome + tcp-proxy）
2. WSL: `cdp-bridge.sh start`

**環境變量**：
- `CHROME_HOST`：Chrome CDP 地址（WSL2 默認 `172.29.16.1`，原生 `127.0.0.1`）
- `CHROME_PORT`：Chrome CDP 端口（WSL2 預設 `9223`，原生 `9222`）
- `CDP_PROXY_PORT`：Bridge 監聽端口（預設 `3456`）

**Hermes 內建 browser 工具 vs CDP Bridge**：
- Hermes browser 工具走 Browserbase 雲端瀏覽器
- CDP Bridge 走用戶真實 Chrome，帶登入態
- 兩者場景不同：需要登入態 → CDP；快速頁面抓取 → Hermes browser

## References 索引

| 文件 | 何時載入 |
|------|---------|
| `references/cdp-api.md` | 需要 CDP API 詳細參考、JS 提取模式、錯誤處理時 |
| `references/site-patterns/{domain}.md` | 確定目標網站後，讀取對應站點經驗 |