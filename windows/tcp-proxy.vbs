' tcp-proxy.vbs — Startup helper for tcp-proxy.js
' Validates dependencies before launching, shows error on failure

Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

proxyPath = fso.BuildPath(ws.ExpandEnvironmentStrings("%USERPROFILE%"), "tcp-proxy.js")

If Not fso.FileExists(proxyPath) Then
    MsgBox "tcp-proxy.js not found at:" & vbCrLf & proxyPath & vbCrLf & vbCrLf & "Run start_chrome_cdp.bat first.", vbExclamation, "CDP Proxy Error"
    WScript.Quit 1
End If

' Run minimized, don't wait
ws.Run "node """ & proxyPath & """", 0, False