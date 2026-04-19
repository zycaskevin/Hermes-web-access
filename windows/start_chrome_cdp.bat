@echo off
:: ============================================
:: Chrome CDP + TCP Proxy 一鍵啟動
:: 雙擊即可，不需要管理員權限
:: ============================================

:: Try to find Chrome (check common locations)
set "CHROME_PATH="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
) else if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
) else (
    echo [ERROR] Chrome not found. Please install or set CHROME_PATH.
    pause
    exit /b 1
)

:: Copy tcp-proxy.js to USERPROFILE if not already there
if not exist "%USERPROFILE%\tcp-proxy.js" (
    echo [SETUP] Copying tcp-proxy.js to %USERPROFILE%...
    copy /Y "%~dp0tcp-proxy.js" "%USERPROFILE%\tcp-proxy.js" >nul
    if errorlevel 1 (
        echo [ERROR] Failed to copy tcp-proxy.js
        pause
        exit /b 1
    )
)

:: Verify Node.js is available
where node >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js not found. Install from https://nodejs.org/
    pause
    exit /b 1
)

echo [1/3] 關閉所有 Chrome 進程...
taskkill /F /IM chrome.exe >nul 2>&1
timeout /t 3 /nobreak >nul

echo [2/3] 啟動 Chrome（CDP 模式）...
start "" "%CHROME_PATH%" --remote-debugging-port=9222 --no-first-run --no-default-browser-check --user-data-dir=%USERPROFILE%\cdp-chrome-profile

echo 等待 Chrome 啟動...
timeout /t 5 /nobreak >nul

echo [3/3] 啟動 TCP Proxy（讓 WSL 可訪問）...
start /min node "%USERPROFILE%\tcp-proxy.js"

echo.
echo ============================================
echo 測試 CDP 端口...
curl -s http://127.0.0.1:9222/json/version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [OK] Chrome CDP port 9222 已開啟
) else (
    echo [WARN] Port 9222 未回應
)
curl -s http://127.0.0.1:9223/json/version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [OK] TCP Proxy port 9223 已開啟（WSL 可訪問）
) else (
    echo [WARN] Port 9223 未回應
)
echo.
echo WSL 端請執行：
echo   ~/.hermes/skills/web-access/scripts/cdp-bridge.sh start
echo.
pause