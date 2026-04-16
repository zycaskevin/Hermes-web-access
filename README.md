# web-access-hp

> **Hermes Agent 原生版** — Fork from [eze-is/web-access](https://github.com/eze-is/web-access) v2.4.3

給 AI Agent 裝上完整聯網能力。基於 [web-access](https://github.com/eze-is/web-access) 的瀏覽哲學與 CDP 架構，針對 **Hermes Agent** 環境重寫後端，新增 WSL2 橋接、Python 原生 Proxy、多平台啟動腳本。

## 與上游的差異

| 項目 | 上游 (Claude Code) | 本 Fork (Hermes) |
|------|-------------------|------------------|
| CDP Proxy 後端 | Node.js (ESM, 需要 Node 22+) | **Python (aiohttp)**, 零 Node 版本依賴 |
| WSL2 支援 | 無 | ✅ Windows→WSL TCP 橋接，三層穿透 |
| Chrome 啟動 | 需要 `chrome://inspect` 手動勾選 | 自動 `--remote-debugging-port=9222` + 獨立 profile |
| API 相容 | cdp-proxy.mjs 原生 API | **完全相容** 相同 HTTP 端點 |
| 額外功能 | — | `cdp-bridge.sh` 管理腳本、Windows 一鍵啟動、健康檢查端點 |
| 最低依賴 | Node.js 22+ / ws 模組 | Python 3.10+ / aiohttp |

## 架構

```
┌─────────────┐     HTTP/WS      ┌──────────────┐     TCP      ┌─────────────┐     HTTP/WS     ┌─────────┐
│ Hermes Agent │ ──────────────→ │ cdp-bridge.py │ ──────────→ │ tcp-proxy.js │ ──────────────→ │ Chrome  │
│  (WSL)       │   :3456         │  (WSL)        │  :9223      │  (Windows)   │   :9222         │ CDP     │
└─────────────┘                  └──────────────┘              └─────────────┘                  └─────────┘
```

**三層橋接**（WSL2 環境必需）：
1. **Chrome** — `--remote-debugging-port=9222` 監聽 `127.0.0.1:9222`
2. **tcp-proxy.js** — Windows Node.js TCP 轉發 `0.0.0.0:9223 → 127.0.0.1:9222`
3. **cdp-bridge.py** — WSL Python HTTP/WS Proxy `0.0.0.0:3456 → 172.29.16.1:9223`

> 原生 Linux / macOS 不需要三層橋接，cdp-bridge.py 可直接連 Chrome `localhost:9222`。

## 快速安裝

**Hermes Agent 安裝：**
```bash
# 克隆到 Hermes skills 目錄
git clone https://github.com/your-username/web-access-hp ~/.hermes/skills/web-access

# 安裝 Python 依賴
pip install aiohttp
```

**原生 Linux/macOS 安裝：**
```bash
git clone https://github.com/your-username/web-access-hp ~/.hermes/skills/web-access
pip install aiohttp
# Chrome 已開啟 CDP 即可使用，無需 tcp-proxy
```

## 啟動

### 方式一：一鍵腳本（推薦）

**Windows (WSL2)**:
1. 雙擊桌面 `start_chrome_cdp.bat` — 啟動 Chrome + tcp-proxy
2. WSL 內執行：
```bash
~/.hermes/skills/web-access/scripts/cdp-bridge.sh start
```

**Linux/macOS**:
```bash
# 確保 Chrome 以 --remote-debugging-port=9222 啟動
~/.hermes/skills/web-access/scripts/cdp-bridge.sh start
```

### 方式二：手動

```bash
# 啟動 CDP Bridge（預設 port 3456）
CHROME_HOST=127.0.0.1 CHROME_PORT=9222 python3 ~/.hermes/skills/web-access/scripts/cdp-bridge.py
```

### 管理命令

```bash
cdp-bridge.sh start     # 啟動
cdp-bridge.sh stop      # 停止
cdp-bridge.sh status    # 狀態
cdp-bridge.sh restart   # 重啟
```

## CDP Bridge API

與上游 cdp-proxy.mjs **完全相容**的 HTTP API：

```bash
# 基礎操作
curl -s http://localhost:3456/targets              # 列出 tab
curl -s http://localhost:3456/json/version           # Chrome 版本
curl -s http://localhost:3456/health                # 健康檢查
curl -s "http://localhost:3456/new?url=https://example.com"  # 新 tab
curl -s "http://localhost:3456/close?target=ID"     # 關閉 tab
curl -s "http://localhost:3456/info?target=ID"       # 頁面資訊

# 頁面操作
curl -s -X POST "http://localhost:3456/eval?target=ID" -d 'document.title'   # 執行 JS
curl -s "http://localhost:3456/navigate?target=ID&url=URL"  # 導航
curl -s "http://localhost:3456/back?target=ID"               # 返回
curl -s -X POST "http://localhost:3456/click?target=ID" -d 'button.submit'   # JS 點擊
curl -s -X POST "http://localhost:3456/clickAt?target=ID" -d '.upload-btn'   # 真實滑鼠點擊
curl -s -X POST "http://localhost:3456/setFiles?target=ID" \
  -d '{"selector":"input[type=file]","files":["/path/to/file.png"]}'  # 文件上傳
curl -s "http://localhost:3456/screenshot?target=ID&file=/tmp/shot.png"  # 截圖
curl -s "http://localhost:3456/scroll?target=ID&direction=bottom"       # 滾動
```

## WSL2 Chrome 設定

Chrome 147+ 的 `--remote-debugging-port` 需要先 kill 所有 Chrome 進程才能生效：

```bash
# Windows PowerShell
taskkill /F /IM chrome.exe
Start-Process "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  -ArgumentList '--remote-debugging-port=9222','--no-first-run','--user-data-dir=C:\Users\User\cdp-chrome-profile'
```

### 開機自啟

1. **tcp-proxy.vbs** — 放入 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`
2. **start_chrome_cdp.bat** — 桌面雙擊啟動

## 瀏覽哲學

延續上游的核心設計：**像人一樣思考，目標驅動而非步驟驅動。**

- 拿到請求 → 先明確成功標準
- 選擇起點 → 驗證最高概率路徑
- 過程校驗 → 結果是證據，不是二元信號
- 完成判斷 → 達標即停，不過度操作

## 致謝

- **[一澤 Eze](https://github.com/eze-is)** — 原版 [web-access](https://github.com/eze-is/web-access) 的作者，瀏覽哲學與 CDP Proxy 架構的設計者
- 上游 MIT 授權，本 Fork 同樣 MIT 授權

## License

MIT