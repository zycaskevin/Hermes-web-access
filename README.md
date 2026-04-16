# Hermes-web-access

> **Hermes Agent 原生聯網技能** — Fork from [eze-is/web-access](https://github.com/eze-is/web-access) v2.4.3

給 Hermes Agent 裝上完整瀏覽器自動化能力。基於上游的瀏覽哲學與 CDP 架構，重寫後端為 Python，原生解決 WSL2 環境下 Chrome CDP 橋接問題。

## 與上游的差異

| 項目 | 上游 (Claude Code) | 本 Fork (Hermes) |
|------|-------------------|------------------|
| CDP Proxy 後端 | Node.js (ESM, 需要 Node 22+) | **Python (aiohttp)**, 零 Node 版本依賴 |
| WSL2 橋接 | 無 | ✅ 三層穿透，auto-detect gateway |
| Chrome 啟動 | 手動 `chrome://inspect` 勾選 | 自動 `--remote-debugging-port=9222` + 獨立 profile |
| API 相容 | cdp-proxy.mjs 原生 API | **完全相容** 相同 HTTP 端點 |
| 額外功能 | — | `cdp-bridge.sh` 管理腳本、Windows 一鍵啟動、健康檢查 |
| 最低依賴 | Node.js 22+ / ws 模組 | Python 3.10+ / aiohttp |

## 架構

Hermes Agent 跑在 WSL2，Chrome 跑在 Windows。WSL2 無法直連 Windows localhost，需三層橋接：

```
┌─────────────┐     HTTP/WS      ┌──────────────┐     TCP      ┌─────────────┐     HTTP/WS     ┌─────────┐
│ Hermes Agent │ ──────────────→ │ cdp-bridge.py │ ──────────→ │ tcp-proxy.js │ ──────────────→ │ Chrome  │
│  (WSL)       │   :3456         │  (WSL)        │  :9223      │  (Windows)   │   :9222         │ CDP     │
└─────────────┘                  └──────────────┘              └─────────────┘                  └─────────┘
```

1. **Chrome** — `--remote-debugging-port=9222` 監聽 `127.0.0.1:9222`
2. **tcp-proxy.js** — Windows TCP 轉發 `0.0.0.0:9223 → 127.0.0.1:9222`
3. **cdp-bridge.py** — WSL HTTP/WS Proxy `0.0.0.0:3456 → <gateway>:9223`

> Gateway IP 由 `cdp-bridge.sh` 自動偵測，無需手動設定。

## 安裝

```bash
# 克隆到 Hermes skills 目錄
git clone https://github.com/zycaskevin/Hermes-web-access ~/.hermes/skills/web-access

# 安裝 Python 依賴
pip install aiohttp

# 將 Windows 腳本複製到桌面
cp ~/.hermes/skills/web-access/windows/* /mnt/c/Users/<YourUsername>/Desktop/
```

## 啟動

### 1. Windows 端

雙擊桌面 `start_chrome_cdp.bat` — 自動完成：
- 終止所有 Chrome 進程
- 以 CDP 模式啟動 Chrome（獨立 profile，不影響日常瀏覽）
- 啟動 TCP Proxy

### 2. WSL 端

```bash
~/.hermes/skills/web-access/scripts/cdp-bridge.sh start    # 啟動
~/.hermes/skills/web-access/scripts/cdp-bridge.sh stop     # 停止
~/.hermes/skills/web-access/scripts/cdp-bridge.sh status   # 狀態
~/.hermes/skills/web-access/scripts/cdp-bridge.sh restart  # 重啟
```

### 開機自啟

將 `windows/tcp-proxy.vbs` 放入 Windows 啟動資料夾：
```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
```

## Chrome CDP 注意事項

Chrome 147+ 的 `--remote-debugging-port` 必須先終止所有 Chrome 進程才能生效。使用獨立 profile (`cdp-chrome-profile`) 避免影響日常 Chrome。這些已整合進 `start_chrome_cdp.bat`。

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

## ⚠️ 使用提醒

通過瀏覽器自動化操作社交平台存在帳號被限流或封禁的風險。**建議使用小號操作。**

## 瀏覽哲學

延續上游的核心設計：**像人一樣思考，目標驅動而非步驟驅動。**

- 拿到請求 → 先明確成功標準
- 選擇起點 → 驗證最高概率路徑
- 過程校驗 → 結果是證據，不是二元信號
- 完成判斷 → 達標即停，不過度操作

詳見 [SKILL.md](./SKILL.md)。

## 致謝

- **[一澤 Eze](https://github.com/eze-is)** — 原版 [web-access](https://github.com/eze-is/web-access) 的作者
- 瀏覽哲學、CDP 架構、站點經驗體系均源自上游
- 本 Fork 以 MIT 授權延續

## License

MIT