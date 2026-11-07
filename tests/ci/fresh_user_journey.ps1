# CI only (job fresh-user-journey in .github/workflows/ci.yml): everything a brand-new user does with Delta on a
# clean Windows PC, from the README's one command to uninstall, against the commit being tested.
#
#   1. the README one-liner under the Restricted policy a new PC has (DELTA_REF = this commit, no launch)
#   2. a NEW Windows PowerShell: delta --version, delta doctor
#   3. Delta opened from its Desktop icon (as a double-click), the dashboard answers
#   4. every dashboard page's first-run API, plus every GET path the dashboard's own scripts call
#   5. first message with NO AI model: a clear "how to add a model" answer, no crash
#   6. onboarding (setup complete), settings save, add/remove a login, a schedule, a memory
#   7. a local model appears (a stand-in for LM Studio, tests/ci/fake_lmstudio.py: no keys): Delta picks it up
#      without a restart, a chat message gets its reply
#   8. a task opens Notepad; a task is stopped while it waits
#   9. delta update (refused while running), delta update --stop, then Delta opened again from its icon
#  10. logs\crashes.log empty, no traceback in the service log, Shut down leaves no process behind
#  11. uninstall -RemoveData: folder, icons, Start-menu folder, Path, data, Delta's saved keys gone; a stranger's
#      saved key with "delta" in its name kept; no startup entry or scheduled task added
# Findings are collected (every check runs) and the job fails at the end if any check failed. Logs go to
# $env:RUNNER_TEMP\fresh-user (uploaded as an artifact).
# Keep this file ASCII: Windows PowerShell 5.1 reads a file without a BOM in the ANSI code page.
param(
    [Parameter(Mandatory = $true)][string]$Ref,
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$Version
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$out = Join-Path $env:RUNNER_TEMP "fresh-user"
New-Item -ItemType Directory -Force $out | Out-Null
$ps51 = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$url = "https://raw.githubusercontent.com/$Repo/$Ref/scripts/install.ps1"
$dir = Join-Path $env:LOCALAPPDATA "Programs\Delta"
$data = Join-Path $env:LOCALAPPDATA "Delta"
$desktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Delta.lnk"
$menuDir = Join-Path ([Environment]::GetFolderPath("Programs")) "Delta"
$menuLnk = Join-Path ([Environment]::GetFolderPath("Programs")) "Delta.lnk"
$base = "http://127.0.0.1:7878"
$newWindow = "`$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + " +
             "[Environment]::GetEnvironmentVariable('Path','User'); "
$script:failures = New-Object System.Collections.Generic.List[string]
$script:token = ""
$script:lastApi = ""
$fake = $null

function Check([bool]$ok, [string]$what) {
    if ($ok) { Write-Host "PASS  $what" -ForegroundColor Green }
    else { Write-Host "FAIL  $what" -ForegroundColor Red; $script:failures.Add($what) }
    return $ok
}
function Must([bool]$ok, [string]$what) {
    if (-not (Check $ok $what)) { Write-Host "      last API call: $script:lastApi"; throw "cannot go on: $what" }
}

function Child([string]$command, [hashtable]$envs = @{}) {
    # A new Windows PowerShell 5.1 window as a user opens it (the README's "Open PowerShell"): no profile, no
    # -ExecutionPolicy, its own PSModulePath.
    $saved = @{}
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    foreach ($k in @($envs.Keys) + "PSModulePath") { $saved[$k] = [Environment]::GetEnvironmentVariable($k) }
    try {
        [Environment]::SetEnvironmentVariable("PSModulePath", $null)
        foreach ($k in $envs.Keys) { [Environment]::SetEnvironmentVariable($k, $envs[$k]) }
        $o = & $ps51 -NoProfile -Command ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding `$false; " +
                                          $command) 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
        $ErrorActionPreference = $eap
    }
    $o | ForEach-Object { Write-Host "    | $_" }
    return [pscustomobject]@{ Code = $code; Out = ($o -join "`n") }
}

function Api([string]$method, [string]$path, $body = $null) {
    $req = @{ Method = $method; Uri = "$base$path"; SkipHttpErrorCheck = $true; UseBasicParsing = $true
               TimeoutSec = 60; Headers = @{ Authorization = "Bearer $script:token" } }
    if ($null -ne $body) { $req["Body"] = ($body | ConvertTo-Json -Depth 8 -Compress); $req["ContentType"] = "application/json" }
    try { $r = Invoke-WebRequest @req } catch {
        $script:lastApi = "$method $path -> no answer: $($_.Exception.Message)"
        return [pscustomobject]@{ Code = 0; Text = "$_"; Json = $null }
    }
    $json = $null
    try { $json = $r.Content | ConvertFrom-Json } catch { }
    $script:lastApi = "$method $path -> $([int]$r.StatusCode): $("$($r.Content)".Substring(0, [Math]::Min(300, "$($r.Content)".Length)))"
    return [pscustomobject]@{ Code = [int]$r.StatusCode; Text = "$($r.Content)"; Json = $json }
}

function PortOpen([int]$port) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { $c.ConnectAsync("127.0.0.1", $port).Wait(300) -and $c.Connected } catch { $false } finally { $c.Dispose() }
}
function WaitFor([scriptblock]$cond, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) { if (& $cond) { return $true }; Start-Sleep -Milliseconds 500 }
    return [bool](& $cond)
}

function OpenFromIcon([string]$why) {
    # A double-click on the Desktop icon (ShellExecute on the .lnk), then wait for the dashboard to answer.
    $t = Get-Date
    Start-Process -FilePath $desktopLnk
    Must (WaitFor { PortOpen 7878 } 180) "Delta opens from its Desktop icon ($why) and the dashboard answers"
    Write-Host ("      dashboard up after {0} s" -f [int]((Get-Date) - $t).TotalSeconds)
    $script:token = (Get-Content (Join-Path $data "api_token") -Raw).Trim()
    Must ((WaitFor { (Api GET "/api/status").Code -eq 200 } 60)) "the API answers with the saved token ($why)"
}

function RunTask([string]$text, [int]$seconds = 120, [string]$chat = "") {
    $r = Api POST "/api/tasks" @{ text = $text; chat_id = $chat }
    if ($r.Code -ne 200) { return [pscustomobject]@{ status = "http $($r.Code)"; answer = $r.Text; asked = 0 } }
    $id = $r.Json.task_id
    $asked = 0
    $deadline = (Get-Date).AddSeconds($seconds)
    $res = $null
    while ((Get-Date) -lt $deadline) {
        foreach ($a in @((Api GET "/api/approvals").Json)) {
            if ($a -and $a.id) { $asked++; Api POST "/api/approvals/$($a.id)" @{ approved = $true } | Out-Null }
        }
        $res = (Api GET "/api/tasks/$id").Json
        if ($res -and $res.status -ne "running") { break }
        Start-Sleep -Seconds 1
    }
    if (-not $res) { $res = [pscustomobject]@{ status = "unknown"; answer = "" } }
    $res | Add-Member -NotePropertyName asked -NotePropertyValue $asked -Force
    $res | Add-Member -NotePropertyName task_id -NotePropertyValue $id -Force
    Write-Host "      task '$text' -> $($res.status): $("$($res.answer)".Substring(0, [Math]::Min(300, "$($res.answer)".Length)))"
    return $res
}

function DeltaProcesses {
    $venv = (Join-Path $dir ".venv").TrimEnd("\") + "\"
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) })
}

function Leftovers {
    # What a program can leave behind that starts it again or shows it to the user.
    $run = @()
    foreach ($key in "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run", "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce") {
        try { $p = Get-ItemProperty $key -ErrorAction Stop; $run += $p.PSObject.Properties | Where-Object { $_.Name -notlike "PS*" } |
              ForEach-Object { "$key :: $($_.Name) = $($_.Value)" } } catch { }
    }
    $startup = @(Get-ChildItem ([Environment]::GetFolderPath("Startup")) -Force -ErrorAction SilentlyContinue | ForEach-Object { "startup :: $($_.Name)" })
    $tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | ForEach-Object { "task :: $($_.TaskPath)$($_.TaskName)" })
    return @($run + $startup + $tasks)
}

function ShowLog([string]$path, [int]$lines = 60) {
    if (Test-Path $path) {
        Write-Host "::group::last $lines lines of $path"
        Get-Content $path -Tail $lines | ForEach-Object { Write-Host "    | $_" }
        Write-Host "::endgroup::"
    }
}

function SaveLogs {
    foreach ($f in @((Join-Path $env:TEMP "delta-install.log"))) { if (Test-Path $f) { Copy-Item $f $out -Force } }
    if (Test-Path (Join-Path $data "logs")) { Copy-Item (Join-Path $data "logs") (Join-Path $out "delta-logs") -Recurse -Force }
}

$before = Leftovers
$cmdTarget = "deltadental-test-$PID"   # a stranger's saved key whose name contains "delta"
try {
    # 1. the README one-liner ---------------------------------------------------------------------------------
    Child "Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy Restricted -Force" @{ PSExecutionPolicyPreference = "Bypass" } | Out-Null
    $t = Get-Date
    $r = Child "Write-Host ('policy: ' + (Get-ExecutionPolicy)); irm '$url' | iex" `
               @{ DELTA_REF = $Ref; DELTA_REPO = $Repo; DELTA_NO_LAUNCH = "1" }
    Must ($r.Code -eq 0 -and $r.Out -match "Delta is installed") ("README one-liner installs Delta under Restricted " +
          "(exit $($r.Code), $([int]((Get-Date) - $t).TotalSeconds) s)")
    Check ($r.Out -match "policy: Restricted") "the one-liner ran under the Restricted policy" | Out-Null
    Copy-Item (Join-Path $env:TEMP "delta-install.log") (Join-Path $out "install-first.log") -Force -ErrorAction SilentlyContinue

    # 2. a new PowerShell ------------------------------------------------------------------------------------
    $v = Child ($newWindow + "delta --version; exit `$LASTEXITCODE")
    Check ($v.Code -eq 0 -and $v.Out -match [regex]::Escape($Version)) "delta --version in a new window prints $Version" | Out-Null
    $d = Child ($newWindow + "delta doctor; exit `$LASTEXITCODE")
    $d.Out | Out-File (Join-Path $out "doctor-first.txt") -Encoding utf8
    Check ($d.Out -notmatch "FAIL ") "delta doctor reports no FAIL on a fresh PC" | Out-Null
    Check ($d.Out -notmatch "Traceback") "delta doctor prints no traceback" | Out-Null
    Check ((Test-Path $desktopLnk) -and (Test-Path $menuLnk) -and (Test-Path (Join-Path $menuDir "Update Delta.lnk"))) `
        "Desktop icon, Start-menu icon and 'Update Delta' exist" | Out-Null

    # 3. open Delta from its icon ----------------------------------------------------------------------------
    OpenFromIcon "first run"

    # 4. every first-run page and API ------------------------------------------------------------------------
    $page = Invoke-WebRequest "$base/" -UseBasicParsing -SkipHttpErrorCheck
    Check ($page.StatusCode -eq 200 -and $page.Content -match "<html") "the dashboard page loads" | Out-Null
    $assets = [regex]::Matches($page.Content, '(?:src|href)="(/(?:ui/[^"#?]+|[a-z]+\.js|favicon\.ico))"') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique
    foreach ($a in $assets) {
        $code = (Invoke-WebRequest "$base$a" -UseBasicParsing -SkipHttpErrorCheck).StatusCode
        Check ($code -eq 200) "dashboard file $a loads (got $code)" | Out-Null
    }
    $pages = "/api/status", "/api/setup", "/api/settings", "/api/security", "/api/telegram", "/api/emergency",
             "/api/apps", "/api/apps/preferred", "/api/connectors", "/api/mcp-servers", "/api/google",
             "/api/chats", "/api/tasks", "/api/approvals", "/api/runs/unfinished", "/api/schedules",
             "/api/documents", "/api/memory", "/api/memory/stats", "/api/skills", "/api/learned", "/api/learn",
             "/api/logins", "/api/watchers", "/api/attachments", "/api/addons", "/api/boundaries", "/api/audit"
    foreach ($p in $pages) {
        $r = Api GET $p
        Check ($r.Code -eq 200) "GET $p on first run (got $($r.Code)$(if ($r.Code -ne 200) { ': ' + $r.Text.Substring(0, [Math]::Min(200, $r.Text.Length)) }))" | Out-Null
    }
    # Every plain GET path the dashboard's own scripts use: none may be missing (404) or crash (5xx) on first run.
    $js = Get-ChildItem (Join-Path $dir "src\delta\api\static") -Filter *.js | ForEach-Object { Get-Content $_.FullName -Raw }
    $used = [regex]::Matches(($js -join "`n"), '["''`](/api/[a-z0-9/_\-]+)["''`]') | ForEach-Object { $_.Groups[1].Value.TrimEnd("/") } |
            Where-Object { $_ -notin $pages -and $_ -notmatch "/(shutdown|stop-all|complete|lock|unlock|pair|forget|signin|key|preview)$" } |
            Sort-Object -Unique
    foreach ($p in $used) {
        $r = Api GET $p
        Check ($r.Code -lt 500 -and $r.Code -ne 404 -and $r.Code -ne 0) "dashboard path $p answers (got $($r.Code))" | Out-Null
    }
    $setup = (Api GET "/api/setup").Json
    Check (-not $setup.completed) "setup is not completed on a fresh PC (the wizard shows)" | Out-Null
    Check (@($setup.models_ready).Count -eq 0) "no AI model is reported ready on a PC with none" | Out-Null
    $status = (Api GET "/api/status").Json
    Check ((@($status.warnings) -join " ") -match "No AI model") "the top bar warns that no AI model is set up" | Out-Null

    # 5. the first message, with no AI model at all ----------------------------------------------------------
    $first = RunTask "hello" 90
    Check ($first.status -in "failed", "done", "answered") "the first message without a model ends (status $($first.status))" | Out-Null
    Check ("$($first.answer)" -match "Groq|Setup|LM Studio|Ollama") "with no model the reply says how to add one (got: $($first.answer))" | Out-Null
    Check ("$($first.answer)" -notmatch "not reachable at|Errors:|http://127") "with no model the reply has no internal error text (got: $($first.answer))" | Out-Null

    # 6. onboarding and settings -----------------------------------------------------------------------------
    $r = Api POST "/api/setup/complete" @{ owner_name = "Fresh User"; autonomy = "ask-risky"; telegram_enabled = $false }
    Check ($r.Code -eq 200 -and $r.Json.ok) "onboarding: setup complete saves (got $($r.Code))" | Out-Null
    Check ((Api GET "/api/setup").Json.completed) "onboarding: setup is completed afterwards" | Out-Null
    $r = Api PUT "/api/settings" @{ memory = @{ auto_extract = $true; learn_skills = $true } }
    Check ($r.Code -eq 200) "Settings: save (got $($r.Code): $($r.Text.Substring(0, [Math]::Min(200, $r.Text.Length))))" | Out-Null
    $r = Api POST "/api/logins" @{ name = "Test portal"; site = "https://portal.example.edu"; username = "student1"; password = "not-a-real-password" }
    Check ($r.Code -eq 200 -and $r.Json.ok) "Settings > Logins: add a login (got $($r.Code): $($r.Text.Substring(0, [Math]::Min(200, $r.Text.Length))))" | Out-Null
    $logins = (Api GET "/api/logins").Json
    Check (@($logins.logins).Count -eq 1 -and $logins.vault) "the saved login is listed and the vault works" | Out-Null
    Check ((Api GET "/api/logins").Text -notmatch "not-a-real-password") "the login list never shows the password" | Out-Null
    if (@($logins.logins).Count -ge 1) { Check ((Api DELETE "/api/logins/$($logins.logins[0].id)").Code -eq 200) "remove the login" | Out-Null }
    $r = Api POST "/api/schedules/preview" @{ text = "every day at 9 am remind me to drink water" }
    Check ($r.Code -eq 200) "Schedules: preview (got $($r.Code))" | Out-Null
    $r = Api POST "/api/schedules" @{ text = "every day at 9 am remind me to drink water"; confirm = $true }
    Check ($r.Code -eq 200 -and $r.Json.id) "Schedules: save (got $($r.Code))" | Out-Null
    if ($r.Json.id) { Check ((Api DELETE "/api/schedules/$($r.Json.id)").Code -eq 200) "Schedules: delete" | Out-Null }
    $r = Api POST "/api/memory" @{ content = "User likes short answers"; kind = "profile" }
    Check ($r.Code -eq 200 -and $r.Json.id) "Memory: add a fact (got $($r.Code))" | Out-Null
    if ($r.Json.id) { Check ((Api DELETE "/api/memory/$($r.Json.id)").Code -eq 200) "Memory: delete it" | Out-Null }

    # 7. a local model appears (LM Studio stand-in) ----------------------------------------------------------
    $fakeLog = Join-Path $out "fake-lmstudio.log"
    $py = (Get-Command python -ErrorAction Stop).Source
    $fake = Start-Process $py -ArgumentList @("`"$PSScriptRoot\fake_lmstudio.py`"", "`"$fakeLog`"") -PassThru -WindowStyle Hidden
    Must (WaitFor { PortOpen 1234 } 30) "the LM Studio stand-in listens on 127.0.0.1:1234"
    Check (WaitFor { @((Api GET "/api/status").Json.providers) -contains "lmstudio" } 75) `
        "Delta sees the newly started local model within 75 s, without a restart" | Out-Null
    $hi = RunTask "hello there" 120
    Check ("$($hi.answer)" -match "FAKE-REPLY-OK") "a chat message gets the model's reply (got $($hi.status): $($hi.answer))" | Out-Null
    $chats = (Api GET "/api/chats").Json
    Check (@($chats).Count -ge 1 -or @($chats.chats).Count -ge 1) "the chat is in the chat list" | Out-Null
    if ($hi.chat_id) {
        $r = Api GET "/api/chats/$($hi.chat_id)"
        Check ($r.Code -eq 200 -and $r.Text -match "FAKE-REPLY-OK") "the chat holds the reply" | Out-Null
    }

    # 8. a task opens Notepad; a task is stopped ------------------------------------------------------------
    Get-Process notepad -ErrorAction SilentlyContinue | Stop-Process -Force
    $np = RunTask "open Notepad" 180
    $opened = WaitFor { [bool](Get-Process notepad -ErrorAction SilentlyContinue) } 20
    Check $opened "a task opens Notepad (task $($np.status), $($np.asked) approval(s): $($np.answer))" | Out-Null
    Check ($np.status -eq "done") "the Notepad task ends done (got $($np.status))" | Out-Null
    Get-Process notepad -ErrorAction SilentlyContinue | Stop-Process -Force
    $r = Api POST "/api/tasks" @{ text = "slowtask: think for a long time" }
    $slow = $r.Json.task_id
    Start-Sleep -Seconds 4
    Check ((Api GET "/api/tasks/$slow").Json.status -eq "running") "the slow task is running" | Out-Null
    $r = Api POST "/api/tasks/$slow/cancel"
    Check ($r.Code -eq 200 -and $r.Json.ok) "Stop on a running task is accepted" | Out-Null
    Check (WaitFor { (Api GET "/api/tasks/$slow").Json.status -eq "cancelled" } 20) `
        "the stopped task ends cancelled (got $((Api GET "/api/tasks/$slow").Json.status))" | Out-Null
    $after = RunTask "hello again" 60
    Check ("$($after.answer)" -match "FAKE-REPLY-OK") "Delta still answers after a stop" | Out-Null
    $r = Api GET "/api/tasks"
    Check ($r.Code -eq 200) "Tasks page lists the history" | Out-Null

    # 9. delta update ---------------------------------------------------------------------------------------
    $u = Child ($newWindow + "delta update; exit `$LASTEXITCODE")
    Check ($u.Code -ne 0 -and $u.Out -match "Delta is running") "plain 'delta update' refuses while Delta runs and says why" | Out-Null
    $installLog = Join-Path $env:TEMP "delta-install.log"
    Remove-Item $installLog -Force -ErrorAction SilentlyContinue
    $u = Child ($newWindow + "delta update --stop; exit `$LASTEXITCODE") @{ DELTA_REF = $Ref; DELTA_REPO = $Repo }
    Check ($u.Code -eq 0) "'delta update --stop' starts the update" | Out-Null
    Start-Sleep -Seconds 5
    $done = WaitFor {
        -not (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match "install\.ps1" -and $_.CommandLine -match "FromGitHub" }) -and
        (Test-Path $installLog) } 900
    $ulog = if (Test-Path $installLog) { Get-Content $installLog -Raw } else { "" }
    Copy-Item $installLog (Join-Path $out "install-update.log") -Force -ErrorAction SilentlyContinue
    Check ($done -and $ulog -match "Delta is installed") "the update finishes and says Delta is installed" | Out-Null
    Check ($ulog -match "closed the running Delta") "the update closed the running Delta itself" | Out-Null
    Check (-not (PortOpen 7878)) "Delta is stopped after the update" | Out-Null
    $v = Child ($newWindow + "delta --version; exit `$LASTEXITCODE")
    Check ($v.Code -eq 0) "delta still works after the update" | Out-Null

    # restart from the icon: data kept, the model still answers
    OpenFromIcon "after the update"
    $setup = (Api GET "/api/setup").Json
    Check ($setup.completed) "setup stays completed after the update" | Out-Null
    $chats = Api GET "/api/chats"
    Check ($chats.Text -match "hello") "old chats are kept after the update" | Out-Null
    $hi2 = RunTask "hello after the update" 120
    Check ("$($hi2.answer)" -match "FAKE-REPLY-OK") "chat works after the update" | Out-Null

    # 10. logs and shutdown ---------------------------------------------------------------------------------
    $crashes = Join-Path $data "logs\crashes.log"
    $crashText = if (Test-Path $crashes) { (Get-Content $crashes -Raw) } else { "" }
    Check (-not "$crashText".Trim()) "logs\crashes.log is empty$(if ("$crashText".Trim()) { ': ' + $crashText.Substring(0, [Math]::Min(1500, $crashText.Length)) })" | Out-Null
    $svc = Join-Path $data "logs\service.log"
    $svcText = if (Test-Path $svc) { Get-Content $svc -Raw } else { "" }
    Check ($svcText -notmatch "Traceback") "the service log has no traceback" | Out-Null
    $r = Api POST "/api/shutdown"
    Check ($r.Code -eq 200) "Shut down is accepted" | Out-Null
    Check (WaitFor { -not (PortOpen 7878) } 30) "the dashboard stops after Shut down" | Out-Null
    Check (WaitFor { @(DeltaProcesses).Count -eq 0 } 30) "no Delta process is left after Shut down (left: $(@(DeltaProcesses | ForEach-Object { $_.CommandLine }) -join ' | '))" | Out-Null

    # 11. uninstall with -RemoveData ------------------------------------------------------------------------
    & "$dir\.venv\Scripts\python.exe" -c "import keyring; keyring.set_password('delta-agent', 'groq_api_key', 'gsk_fake_for_ci')"
    & cmdkey.exe "/generic:$cmdTarget" /user:someone /pass:not-delta | Out-Null
    SaveLogs
    $r = Child "powershell -NoProfile -ExecutionPolicy Bypass -File `"$dir\scripts\uninstall.ps1`" -RemoveData; exit `$LASTEXITCODE"
    Check ($r.Code -eq 0) "uninstall.ps1 -RemoveData exits 0" | Out-Null
    Check (-not (Test-Path $dir)) "the install folder is gone" | Out-Null
    Check (-not (Test-Path $data)) "the data folder is gone (-RemoveData)" | Out-Null
    Check (-not (Test-Path $desktopLnk) -and -not (Test-Path $menuLnk) -and -not (Test-Path $menuDir)) "Desktop icon, Start-menu icon and folder are gone" | Out-Null
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    Check (-not ("$userPath" -split ";" | Where-Object { $_ -like "*Programs\Delta*" })) "nothing of Delta is left on the user Path" | Out-Null
    $creds = (& cmdkey.exe /list) -join "`n"
    Check ($creds -notmatch "delta-agent") "Delta's saved keys are removed" | Out-Null
    Check ($creds -match [regex]::Escape($cmdTarget)) "a saved key of another program (named '$cmdTarget') is NOT removed" | Out-Null
    $new = @(Leftovers | Where-Object { $_ -notin $before })
    Check ($new.Count -eq 0) "no startup entry, Run key or scheduled task was added (new: $($new -join '; '))" | Out-Null
    Check (@(DeltaProcesses).Count -eq 0) "no Delta process runs after uninstall" | Out-Null
} catch {
    $script:failures.Add("stopped: $($_.Exception.Message)")
    Write-Host "STOPPED  $($_.Exception.Message)" -ForegroundColor Red
    Write-Host $_.ScriptStackTrace
    ShowLog (Join-Path $data "logs\service.log") 80
    ShowLog (Join-Path $data "logs\crashes.log") 80
    ShowLog (Join-Path $data "logs\launcher.log") 40
} finally {
    if ($fake) { Stop-Process -Id $fake.Id -Force -ErrorAction SilentlyContinue }
    & cmdkey.exe "/delete:$cmdTarget" 2>$null | Out-Null
    Get-Process notepad -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    SaveLogs
    Child "Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy Undefined -Force" @{ PSExecutionPolicyPreference = "Bypass" } | Out-Null
}
$summary = if ($script:failures.Count) { "Fresh-user journey: $($script:failures.Count) check(s) failed`n" + (($script:failures | ForEach-Object { "- $_" }) -join "`n") } else { "Fresh-user journey: every check passed" }
$summary | Out-File (Join-Path $out "summary.md") -Encoding utf8
if ($env:GITHUB_STEP_SUMMARY) { $summary | Out-File -Append -Encoding utf8 $env:GITHUB_STEP_SUMMARY }
Write-Host "`n$summary"
if ($script:failures.Count) { exit 1 }
