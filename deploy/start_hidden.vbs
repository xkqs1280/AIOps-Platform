' Hidden launcher for AIOpsServer.exe
' - Waits until PostgreSQL (127.0.0.1:5432) accepts connections before
'   starting the backend, so a reboot that starts the app before PG
'   is ready no longer ends up with an empty device list.
' - Launches the backend with SW_HIDE (0): no console window stays on
'   the desktop; closing the launcher does NOT stop the backend.
' Pure ASCII to avoid encoding issues.
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
' server exe is one level above this deploy folder
serverExe = fso.BuildPath(fso.GetParentFolderName(baseDir), "AIOpsServer.exe")
If Not fso.FileExists(serverExe) Then
    serverExe = fso.BuildPath(baseDir, "AIOpsServer.exe")
End If
If Not fso.FileExists(serverExe) Then
    WScript.Quit 1
End If

' --- Wait for PostgreSQL (max ~90s) --------------------------------------
' Probe 127.0.0.1:5432 with a lightweight TCP connect via PowerShell.
Dim pgReady
pgReady = False
Dim i
For i = 1 To 18
    Set exec = WshShell.Exec("powershell -NoProfile -Command ""try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',5432);'UP'}catch{'DOWN'}""")
    Do While exec.Status = 0
        WScript.Sleep 100
    Loop
    If InStr(exec.StdOut.ReadAll, "UP") > 0 Then
        pgReady = True
        Exit For
    End If
    WScript.Sleep 5000
Next

' --- Launch backend hidden -------------------------------------------------
WshShell.CurrentDirectory = fso.GetParentFolderName(serverExe)
WshShell.Run """" & serverExe & """", 0, False
