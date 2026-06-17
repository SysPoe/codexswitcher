[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Position = 0)]
    [string]$Command = "tui",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments,

    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { "~/.codex" }),
    [string]$Store = "~/.codex-switcher",
    [string]$CodexBin = "codex",
    [switch]$RestartApp
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Arguments = @($Arguments)
$script:TuiFrameBuffer = $null
$script:TuiFrameLines = $null
$script:TuiLastFrameLines = $null
$script:TuiFrameHeight = 0
$script:TuiLastFrameHeight = 0
$script:TuiFrameWidth = 0
$script:TuiLastFrameWidth = 0

function Resolve-UserPath([string]$Path) {
    if ($Path -eq "~") {
        $Path = $HOME
    }
    elseif ($Path.StartsWith("~/") -or $Path.StartsWith("~\")) {
        $Path = Join-Path $HOME $Path.Substring(2)
    }
    return [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path))
}

function Ensure-Directory([string]$Path) {
    [void][System.IO.Directory]::CreateDirectory($Path)
}

function Write-Utf8Atomic([string]$Path, [string]$Text) {
    Ensure-Directory (Split-Path -Parent $Path)
    $temp = Join-Path (Split-Path -Parent $Path) (".{0}.{1}.tmp" -f (Split-Path -Leaf $Path), [guid]::NewGuid())
    try {
        [System.IO.File]::WriteAllText($temp, $Text, [System.Text.UTF8Encoding]::new($false))
        [System.IO.File]::Move($temp, $Path, $true)
    }
    finally {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force
        }
    }
}

function Copy-FileAtomic([string]$Source, [string]$Target) {
    Ensure-Directory (Split-Path -Parent $Target)
    $temp = Join-Path (Split-Path -Parent $Target) (".{0}.{1}.tmp" -f (Split-Path -Leaf $Target), [guid]::NewGuid())
    try {
        Copy-Item -LiteralPath $Source -Destination $temp -Force
        [System.IO.File]::Move($temp, $Target, $true)
    }
    finally {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force
        }
    }
}

function Test-ContextName([string]$Name) {
    if ($Name -notmatch '^[A-Za-z0-9_.-]+$') {
        throw "context names may only contain letters, numbers, dot, underscore, and hyphen"
    }
}

function Test-ProviderId([string]$ProviderId) {
    if ($ProviderId -notmatch '^[A-Za-z0-9_-]+$') {
        throw "provider ids may only contain letters, numbers, underscore, and hyphen"
    }
}

function Get-OptionValue([string[]]$Items, [string]$Name, [string]$Default = $null) {
    for ($i = 0; $i -lt $Items.Count; $i++) {
        if ($Items[$i] -eq $Name) {
            if ($i + 1 -ge $Items.Count) { throw "$Name requires a value" }
            return $Items[$i + 1]
        }
    }
    return $Default
}

function Test-Option([string[]]$Items, [string]$Name) {
    return $Items -contains $Name
}

function Get-PositionalArguments([string[]]$Items) {
    $result = [System.Collections.Generic.List[string]]::new()
    $valueOptions = @(
        "--provider-id", "--model", "--base-url", "--provider-name",
        "--wire-api", "--env-key", "--api-key", "--reasoning-effort",
        "--codex-home", "--store", "--codex-bin"
    )
    for ($i = 0; $i -lt $Items.Count; $i++) {
        if ($Items[$i] -eq "--") {
            for ($j = $i + 1; $j -lt $Items.Count; $j++) { $result.Add($Items[$j]) }
            break
        }
        if ($valueOptions -contains $Items[$i]) {
            $i++
            continue
        }
        if (-not $Items[$i].StartsWith("-")) {
            $result.Add($Items[$i])
        }
    }
    return $result.ToArray()
}

function Remove-GlobalOptions([string[]]$Items) {
    $result = [System.Collections.Generic.List[string]]::new()
    $separatorRemoved = $false
    for ($i = 0; $i -lt $Items.Count; $i++) {
        if ($Items[$i] -in @("--codex-home", "--store", "--codex-bin")) {
            $i++
            continue
        }
        if ($Items[$i] -eq "--restart-app" -or $Items[$i] -eq "--") {
            continue
        }
        if ($Items[$i] -eq "-" -and -not $separatorRemoved) {
            $separatorRemoved = $true
            continue
        }
        $result.Add($Items[$i])
    }
    return $result.ToArray()
}

function Set-TopLevelTomlValue([string]$Text, [string]$Key, [string]$Value, [switch]$Remove) {
    $lines = @($Text -split "`r?`n")
    $firstTable = $lines.Count
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\s*\[') {
            $firstTable = $i
            break
        }
    }

    $found = $false
    $output = [System.Collections.Generic.List[string]]::new()
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($i -lt $firstTable -and $lines[$i] -match ("^\s*" + [regex]::Escape($Key) + "\s*=")) {
            if (-not $Remove -and -not $found) {
                $output.Add("$Key = $Value")
            }
            $found = $true
            continue
        }
        if ($i -eq $firstTable -and -not $Remove -and -not $found) {
            $output.Add("$Key = $Value")
            if ($lines[$i] -ne "") { $output.Add("") }
            $found = $true
        }
        $output.Add($lines[$i])
    }
    if (-not $Remove -and -not $found) {
        if ($output.Count -gt 0 -and $output[$output.Count - 1] -ne "") { $output.Add("") }
        $output.Add("$Key = $Value")
    }
    return ($output -join "`n").TrimEnd() + "`n"
}

function Quote-Toml([string]$Value) {
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function Set-TomlTable([string]$Text, [string]$TableName, [System.Collections.IDictionary]$Values) {
    $lines = @($Text -split "`r?`n")
    $output = [System.Collections.Generic.List[string]]::new()
    $insideTarget = $false
    foreach ($line in $lines) {
        if ($line -match '^\s*\[(.+)\]\s*$') {
            $insideTarget = $Matches[1] -eq $TableName
            if ($insideTarget) { continue }
        }
        if (-not $insideTarget) { $output.Add($line) }
    }
    while ($output.Count -gt 0 -and $output[$output.Count - 1] -eq "") {
        $output.RemoveAt($output.Count - 1)
    }
    if ($output.Count -gt 0) { $output.Add("") }
    $output.Add("[$TableName]")
    foreach ($entry in $Values.GetEnumerator()) {
        $value = if ($entry.Value -is [bool]) {
            $entry.Value.ToString().ToLowerInvariant()
        } else {
            Quote-Toml ([string]$entry.Value)
        }
        $output.Add("$($entry.Key) = $value")
    }
    return ($output -join "`n").TrimEnd() + "`n"
}

function Merge-ActiveModelSettings([string]$TargetText, [string]$ActiveText) {
    $activeLines = @($ActiveText -split "`r?`n")
    $modelAssignments = [ordered]@{}
    foreach ($line in $activeLines) {
        if ($line -match '^\s*\[') { break }
        if ($line -match '^\s*(model(?:_[A-Za-z0-9_-]+)?)\s*=.*$' -and $Matches[1] -ne "model_provider") {
            $modelAssignments[$Matches[1]] = $line.Trim()
        }
    }

    $targetLines = @($TargetText -split "`r?`n")
    $filtered = [System.Collections.Generic.List[string]]::new()
    $inTopLevel = $true
    foreach ($line in $targetLines) {
        if ($line -match '^\s*\[') { $inTopLevel = $false }
        if ($inTopLevel -and $line -match '^\s*(model(?:_[A-Za-z0-9_-]+)?)\s*=.*$' -and $Matches[1] -ne "model_provider") {
            continue
        }
        $filtered.Add($line)
    }
    $text = ($filtered -join "`n").TrimEnd() + "`n"
    foreach ($entry in $modelAssignments.GetEnumerator()) {
        $value = $entry.Value.Substring($entry.Value.IndexOf("=") + 1).Trim()
        $text = Set-TopLevelTomlValue $text $entry.Key $value
    }
    return $text
}

function Get-TopLevelTomlValue([string]$Path, [string]$Key, [string]$Default = "") {
    if (-not (Test-Path -LiteralPath $Path)) { return $Default }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*\[') { break }
        if ($line -match ("^\s*" + [regex]::Escape($Key) + "\s*=\s*['""]?(.*?)['""]?\s*(?:#.*)?$")) {
            return $Matches[1]
        }
    }
    return $Default
}

function Get-CodexCommand([string]$Requested) {
    if ([System.IO.Path]::IsPathRooted($Requested) -or $Requested.Contains("\") -or $Requested.Contains("/")) {
        if (-not (Test-Path -LiteralPath $Requested)) { throw "Codex executable not found: $Requested" }
        return $Requested
    }
    $command = Get-Command $Requested -ErrorAction SilentlyContinue
    if ($command) {
        if ($IsWindows -and $command.CommandType -eq "ExternalScript") {
            $cmdSibling = [System.IO.Path]::ChangeExtension($command.Source, ".cmd")
            if (Test-Path -LiteralPath $cmdSibling) { return $cmdSibling }
        }
        return $command.Source
    }
    if ($IsWindows) {
        $cmd = Get-Command "$Requested.cmd" -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw "Codex executable '$Requested' was not found on PATH"
}

function Backup-ActiveFiles([string]$Reason) {
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $backup = Join-Path $script:BackupsDir "$stamp-$Reason"
    $suffix = 1
    while (Test-Path -LiteralPath $backup) {
        $backup = Join-Path $script:BackupsDir "$stamp-$Reason-$suffix"
        $suffix++
    }
    Ensure-Directory $backup
    foreach ($name in @("config.toml", "auth.json")) {
        $source = Join-Path $script:CodexHomePath $name
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $backup }
    }
}

function Get-ContextDirectory([string]$Name, [switch]$Require) {
    Test-ContextName $Name
    $path = Join-Path $script:ContextsDir $Name
    if ($Require -and -not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "context '$Name' does not exist"
    }
    return $path
}

function Capture-Context([string]$Name, [switch]$Overwrite) {
    $context = Get-ContextDirectory $Name
    $config = Join-Path $script:CodexHomePath "config.toml"
    if (-not (Test-Path -LiteralPath $config)) { throw "$config does not exist" }
    if (Test-Path -LiteralPath $context) {
        if (-not $Overwrite) { throw "context '$Name' already exists; use --overwrite" }
        Remove-Item -LiteralPath $context -Recurse -Force
    }
    Ensure-Directory $context
    $configText = [System.IO.File]::ReadAllText($config)
    $configText = Set-TopLevelTomlValue $configText "cli_auth_credentials_store" (Quote-Toml "file")
    Write-Utf8Atomic (Join-Path $context "config.toml") $configText
    $auth = Join-Path $script:CodexHomePath "auth.json"
    if (Test-Path -LiteralPath $auth) { Copy-FileAtomic $auth (Join-Path $context "auth.json") }
    Write-Utf8Atomic (Join-Path $context "metadata.json") (([ordered]@{
        name = $Name
        source = "capture"
        updated_at = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json) + "`n")
}

function Use-Context([string]$Name, [switch]$KeepAuth) {
    $context = Get-ContextDirectory $Name -Require
    $contextConfig = Join-Path $context "config.toml"
    if (-not (Test-Path -LiteralPath $contextConfig)) { throw "context '$Name' has no config.toml" }
    Ensure-Directory $script:CodexHomePath
    Backup-ActiveFiles "use-$Name"

    $targetText = [System.IO.File]::ReadAllText($contextConfig)
    $provider = Get-TopLevelTomlValue $contextConfig "model_provider" "openai"
    $providerAuth = Get-ProviderAuthSummary $contextConfig $provider
    $contextAuth = Join-Path $context "auth.json"
    if ($providerAuth -eq "codex-auth" -and -not (Test-Path -LiteralPath $contextAuth)) {
        throw "context '$Name' requires Codex/OpenAI auth but has no saved auth.json; run 'cdxsw login $Name' first"
    }
    $targetText = Set-TopLevelTomlValue $targetText "cli_auth_credentials_store" (Quote-Toml "file")
    $activeConfig = Join-Path $script:CodexHomePath "config.toml"
    if (Test-Path -LiteralPath $activeConfig) {
        $targetText = Merge-ActiveModelSettings $targetText ([System.IO.File]::ReadAllText($activeConfig))
    }
    Write-Utf8Atomic $activeConfig $targetText

    $activeAuth = Join-Path $script:CodexHomePath "auth.json"
    if (Test-Path -LiteralPath $contextAuth) {
        Copy-FileAtomic $contextAuth $activeAuth
    }
    elseif ((Test-Path -LiteralPath $activeAuth) -and -not $KeepAuth) {
        Remove-Item -LiteralPath $activeAuth -Force
    }
    Write-Utf8Atomic $script:ActiveFile (([ordered]@{
        name = $Name
        codex_home = $script:CodexHomePath
        switched_at = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json) + "`n")
}

function Get-ActiveContext {
    if (-not (Test-Path -LiteralPath $script:ActiveFile)) { return $null }
    try {
        $data = Get-Content -LiteralPath $script:ActiveFile -Raw | ConvertFrom-Json
        if ($data.codex_home -eq $script:CodexHomePath) { return [string]$data.name }
    }
    catch {}
    return $null
}

function Get-Contexts {
    $active = Get-ActiveContext
    $rows = @()
    foreach ($directory in @(Get-ChildItem -LiteralPath $script:ContextsDir -Directory | Sort-Object Name)) {
        $config = Join-Path $directory.FullName "config.toml"
        $provider = Get-TopLevelTomlValue $config "model_provider" "openai"
        $limitSummary = Get-CachedLimitSummary $directory.FullName $provider (Test-Path -LiteralPath (Join-Path $directory.FullName "auth.json"))
        $rows += [pscustomobject]@{
            Active = $(if ($directory.Name -eq $active) { "*" } else { "" })
            Name = $directory.Name
            Model = Get-TopLevelTomlValue $config "model"
            Provider = $provider
            Auth = $(if (Test-Path -LiteralPath (Join-Path $directory.FullName "auth.json")) { "yes" } else { "no" })
            ProviderAuth = Get-ProviderAuthSummary $config $provider
            FiveHour = $limitSummary.FiveHour
            Weekly = $limitSummary.Weekly
            FiveHourDetail = $limitSummary.FiveHourDetail
            WeeklyDetail = $limitSummary.WeeklyDetail
        }
    }
    return $rows
}

function Get-CachedLimitSummary([string]$ContextDirectory, [string]$Provider, [bool]$HasAuth) {
    if ($Provider -ne "openai") {
        return [pscustomobject]@{
            FiveHour = "n/a"; Weekly = "n/a"
            FiveHourDetail = "provider does not use Codex/OpenAI auth"
            WeeklyDetail = "provider does not use Codex/OpenAI auth"
        }
    }
    if (-not $HasAuth) {
        return [pscustomobject]@{
            FiveHour = "not signed in"; Weekly = "not signed in"
            FiveHourDetail = "no auth.json saved for this context"
            WeeklyDetail = "no auth.json saved for this context"
        }
    }
    $cachePath = Join-Path $ContextDirectory "rate_limits.json"
    if (-not (Test-Path -LiteralPath $cachePath)) {
        return [pscustomobject]@{
            FiveHour = "?"; Weekly = "?"
            FiveHourDetail = "not fetched yet; press r to refresh"
            WeeklyDetail = "not fetched yet; press r to refresh"
        }
    }
    try {
        $cache = Get-Content -LiteralPath $cachePath -Raw | ConvertFrom-Json
        $snapshot = $cache.snapshot
        return [pscustomobject]@{
            FiveHour = Format-LimitShort $snapshot.primary
            Weekly = Format-LimitShort $snapshot.secondary
            FiveHourDetail = Format-LimitDetail "five-hour" $snapshot.primary
            WeeklyDetail = Format-LimitDetail "weekly" $snapshot.secondary
        }
    }
    catch {
        return [pscustomobject]@{
            FiveHour = "?"; Weekly = "?"
            FiveHourDetail = "invalid rate-limit cache; press r to refresh"
            WeeklyDetail = "invalid rate-limit cache; press r to refresh"
        }
    }
}

function Update-ContextRateLimits([string]$Name) {
    $context = Get-ContextDirectory $Name -Require
    $configPath = Join-Path $context "config.toml"
    $provider = Get-TopLevelTomlValue $configPath "model_provider" "openai"
    if ((Get-ProviderAuthSummary $configPath $provider) -ne "codex-auth") {
        throw "context '$Name' does not use Codex/OpenAI rate limits"
    }
    $authPath = Join-Path $context "auth.json"
    if (-not (Test-Path -LiteralPath $authPath)) {
        throw "context '$Name' has no saved auth.json"
    }

    $isolatedHome = Join-Path $script:HomesDir $Name
    Ensure-Directory $isolatedHome
    Copy-FileAtomic $configPath (Join-Path $isolatedHome "config.toml")
    Copy-FileAtomic $authPath (Join-Path $isolatedHome "auth.json")
    $codex = Get-CodexCommand $script:CodexBinPath

    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $(if ($env:ComSpec) { $env:ComSpec } else { "cmd.exe" })
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true
    $start.Environment["CODEX_HOME"] = $isolatedHome
    $start.Arguments = '/d /s /c ""' + $codex + '" app-server --stdio"'

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $start
    [void]$process.Start()
    try {
        $initialize = @{
            jsonrpc = "2.0"; id = 1; method = "initialize"
            params = @{ clientInfo = @{ name = "codexswitcher"; version = "0" }; protocolVersion = "2" }
        } | ConvertTo-Json -Compress -Depth 8
        $request = @{
            jsonrpc = "2.0"; id = 2; method = "account/rateLimits/read"; params = $null
        } | ConvertTo-Json -Compress -Depth 8
        $process.StandardInput.WriteLine($initialize)
        $process.StandardInput.WriteLine($request)
        $process.StandardInput.Flush()

        $deadline = [DateTime]::UtcNow.AddSeconds(12)
        $snapshot = $null
        while ([DateTime]::UtcNow -lt $deadline) {
            $remaining = [Math]::Max(1, [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds)
            $readTask = $process.StandardOutput.ReadLineAsync()
            if (-not $readTask.Wait($remaining)) { break }
            $line = $readTask.Result
            if (-not $line) {
                if ($process.HasExited) { break }
                continue
            }
            try { $payload = $line | ConvertFrom-Json }
            catch { continue }
            $idProperty = $payload.PSObject.Properties["id"]
            if ($null -eq $idProperty -or $idProperty.Value -ne 2) { continue }
            $errorProperty = $payload.PSObject.Properties["error"]
            if ($null -ne $errorProperty -and $null -ne $errorProperty.Value) {
                $messageProperty = $errorProperty.Value.PSObject.Properties["message"]
                throw $(if ($messageProperty) { [string]$messageProperty.Value } else { [string]$errorProperty.Value })
            }
            $resultProperty = $payload.PSObject.Properties["result"]
            if ($null -eq $resultProperty -or $null -eq $resultProperty.Value) {
                throw "rate-limit response did not include a result"
            }
            $result = $resultProperty.Value
            $byIdProperty = $result.PSObject.Properties["rateLimitsByLimitId"]
            $limitsProperty = $result.PSObject.Properties["rateLimits"]
            if ($null -ne $byIdProperty -and $null -ne $byIdProperty.Value) {
                $codexProperty = $byIdProperty.Value.PSObject.Properties["codex"]
                if ($null -ne $codexProperty) { $snapshot = $codexProperty.Value }
            }
            if ($null -eq $snapshot -and $null -ne $limitsProperty) {
                $snapshot = $limitsProperty.Value
            }
            break
        }
        if ($null -eq $snapshot) {
            $errorText = $process.StandardError.ReadToEnd()
            throw "timed out reading rate limits$(if ($errorText) { ': ' + $errorText.Trim() } else { '' })"
        }
        Write-Utf8Atomic (Join-Path $context "rate_limits.json") (([ordered]@{
            fetched_at = [DateTime]::UtcNow.ToString("o")
            snapshot = $snapshot
        } | ConvertTo-Json -Depth 12) + "`n")
    }
    finally {
        try { $process.StandardInput.Close() } catch {}
        if (-not $process.HasExited) {
            [void]$process.Kill($true)
            [void]$process.WaitForExit(2000)
        }
        $process.Dispose()
    }
}

function Format-LimitShort($Window) {
    if ($null -eq $Window -or $null -eq $Window.usedPercent) { return "?" }
    return "$([Math]::Round([double]$Window.usedPercent))%"
}

function Format-LimitDetail([string]$Label, $Window) {
    if ($null -eq $Window -or $null -eq $Window.usedPercent) { return "${Label}: unavailable" }
    $text = "${Label}: $([Math]::Round([double]$Window.usedPercent))% used"
    if ($null -ne $Window.resetsAt) {
        $reset = [DateTimeOffset]::FromUnixTimeSeconds([long]$Window.resetsAt).ToLocalTime()
        $text += "; resets $($reset.ToString('yyyy-MM-dd HH:mm'))"
    }
    return $text
}

function Get-ProviderAuthSummary([string]$ConfigPath, [string]$Provider) {
    if ($Provider -eq "openai") { return "codex-auth" }
    if (-not (Test-Path -LiteralPath $ConfigPath)) { return "none" }
    $text = [System.IO.File]::ReadAllText($ConfigPath)
    $escaped = [regex]::Escape("model_providers.$Provider")
    $match = [regex]::Match($text, "(?ms)^\[$escaped\]\s*(.*?)(?=^\[|\z)")
    if (-not $match.Success) { return "none" }
    $body = $match.Groups[1].Value
    if ($body -match '(?m)^\s*requires_openai_auth\s*=\s*true') { return "codex-auth" }
    if ($body -match '(?m)^\s*env_key\s*=\s*["'']([^"'']+)') { return "env:$($Matches[1])" }
    if ($body -match '(?m)^\s*experimental_bearer_token\s*=') { return "bearer-token" }
    return "none"
}

function New-ProviderContext(
    [string]$Name,
    [string]$ProviderId,
    [string]$Model,
    [string]$ProviderName,
    [string]$BaseUrl,
    [string]$WireApi,
    [string]$ApiKey,
    [string]$EnvKey,
    [switch]$RequiresOpenAiAuth,
    [switch]$Overwrite
) {
    Test-ContextName $Name
    Test-ProviderId $ProviderId
    $context = Get-ContextDirectory $Name
    if ((Test-Path -LiteralPath $context) -and -not $Overwrite) {
        throw "context '$Name' already exists; use --overwrite"
    }
    $activeConfig = Join-Path $script:CodexHomePath "config.toml"
    $configText = if (Test-Path -LiteralPath $activeConfig) {
        [System.IO.File]::ReadAllText($activeConfig)
    } else {
        ""
    }
    $configText = Set-TopLevelTomlValue $configText "model_provider" (Quote-Toml $ProviderId)
    $configText = Set-TopLevelTomlValue $configText "model" (Quote-Toml $Model)
    $configText = Set-TopLevelTomlValue $configText "openai_base_url" "" -Remove

    if ($ProviderId -eq "openai") {
        if ($BaseUrl) {
            $configText = Set-TopLevelTomlValue $configText "openai_base_url" (Quote-Toml $BaseUrl)
        }
    }
    elseif ($ProviderId -notin @("ollama", "lmstudio", "amazon-bedrock")) {
        if (-not $BaseUrl) { throw "custom providers require --base-url" }
        $values = [ordered]@{
            name = $(if ($ProviderName) { $ProviderName } else { $ProviderId })
            base_url = $BaseUrl
            wire_api = $(if ($WireApi) { $WireApi } else { "responses" })
            supports_websockets = $false
        }
        if ($EnvKey) { $values["env_key"] = $EnvKey }
        elseif ($ApiKey) { $values["experimental_bearer_token"] = $ApiKey }
        elseif ($RequiresOpenAiAuth) { $values["requires_openai_auth"] = $true }
        $configText = Set-TomlTable $configText "model_providers.$ProviderId" $values
    }

    if (Test-Path -LiteralPath $context) { Remove-Item -LiteralPath $context -Recurse -Force }
    Ensure-Directory $context
    Write-Utf8Atomic (Join-Path $context "config.toml") $configText
    Write-Utf8Atomic (Join-Path $context "metadata.json") (([ordered]@{
        name = $Name
        source = "provider"
        updated_at = [DateTime]::UtcNow.ToString("o")
        model = $Model
        model_provider = $ProviderId
        auth_json = $false
    } | ConvertTo-Json) + "`n")
}

function Invoke-CodexLogin([string]$Name, [string[]]$Options, [string]$SecretInput = $null) {
    $context = Get-ContextDirectory $Name
    Ensure-Directory $context
    $configPath = Join-Path $context "config.toml"
    $activeConfig = Join-Path $script:CodexHomePath "config.toml"
    $configText = if (Test-Path -LiteralPath $configPath) {
        [System.IO.File]::ReadAllText($configPath)
    } elseif (Test-Path -LiteralPath $activeConfig) {
        [System.IO.File]::ReadAllText($activeConfig)
    } else {
        "model_provider = `"openai`"`n"
    }
    $provider = Get-OptionValue $Options "--provider-id" "openai"
    $model = Get-OptionValue $Options "--model" "gpt-5.5"
    $baseUrl = Get-OptionValue $Options "--base-url"
    $configText = Set-TopLevelTomlValue $configText "model_provider" (Quote-Toml $provider)
    $configText = Set-TopLevelTomlValue $configText "model" (Quote-Toml $model)
    $configText = Set-TopLevelTomlValue $configText "cli_auth_credentials_store" (Quote-Toml "file")
    if ($baseUrl) {
        $configText = Set-TopLevelTomlValue $configText "openai_base_url" (Quote-Toml $baseUrl)
    }
    Write-Utf8Atomic $configPath $configText

    $isolatedHome = Join-Path $script:HomesDir $Name
    Ensure-Directory $isolatedHome
    Copy-FileAtomic $configPath (Join-Path $isolatedHome "config.toml")
    $savedAuth = Join-Path $context "auth.json"
    if (Test-Path -LiteralPath $savedAuth) { Copy-FileAtomic $savedAuth (Join-Path $isolatedHome "auth.json") }

    $codex = Get-CodexCommand $script:CodexBinPath
    $loginArgs = @("login")
    foreach ($flag in @("--device-auth", "--with-api-key", "--with-access-token")) {
        if (Test-Option $Options $flag) { $loginArgs += $flag }
    }
    $oldHome = $env:CODEX_HOME
    try {
        $env:CODEX_HOME = $isolatedHome
        if ($null -ne $SecretInput) {
            $SecretInput | & $codex @loginArgs
        } else {
            & $codex @loginArgs
        }
        if ($LASTEXITCODE -ne 0) { throw "codex login failed with exit code $LASTEXITCODE" }
    }
    finally {
        $env:CODEX_HOME = $oldHome
    }
    $newAuth = Join-Path $isolatedHome "auth.json"
    if (-not (Test-Path -LiteralPath $newAuth)) {
        throw "codex login succeeded but did not write auth.json"
    }
    Copy-FileAtomic $newAuth $savedAuth
    if (Test-Option $Options "--use") {
        Use-Context $Name
    }
}

function Restart-CodexApp {
    $running = @(Get-Process -Name "Codex" -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        $running | Stop-Process -Force
        Start-Sleep -Milliseconds 750
    }
    if (-not $IsWindows) { return }
    $package = Get-AppxPackage -Name "OpenAI.Codex" -ErrorAction SilentlyContinue
    if ($package) {
        Start-Process explorer.exe "shell:AppsFolder\$($package.PackageFamilyName)!App"
        return
    }
    # Microsoft Store package family used by the official Windows Codex app.
    try {
        Start-Process explorer.exe "shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
        return
    }
    catch {}
    $localApp = Join-Path $env:LOCALAPPDATA "Programs\Codex\Codex.exe"
    if (Test-Path -LiteralPath $localApp) {
        Start-Process $localApp
        return
    }
    Write-Warning "Codex app was stopped but could not be located for relaunch."
}

function Write-Ansi([string]$Text) {
    if ($null -ne $script:TuiFrameBuffer) {
        [void]$script:TuiFrameBuffer.Append($Text)
        return
    }
    [Console]::Write($Text)
}

function Flush-Tui {
    [Console]::Out.Flush()
}

function Begin-TuiFrame([int]$Height = 0, [int]$ClearFromRow = 1) {
    $script:TuiFrameBuffer = $null
    $script:TuiFrameHeight = $Height
    $script:TuiFrameWidth = [Math]::Max(1, [Console]::WindowWidth - 1)
    $script:TuiFrameLines = [string[]]::new($Height)
    for ($row = [Math]::Max(1, $ClearFromRow); $row -le $Height; $row++) {
        $script:TuiFrameLines[$row - 1] = "".PadRight($script:TuiFrameWidth)
    }
}

function End-TuiFrame {
    if ($null -eq $script:TuiFrameLines) { return }
    $frame = [System.Text.StringBuilder]::new()
    # Windows Terminal supports synchronized output. Repaint every retained row
    # in-place without a screen clear. This avoids flash while ensuring stale
    # list/detail/popup rows from previous frames cannot remain visible.
    [void]$frame.Append("$([char]27)[?2026h")
    for ($row = 1; $row -le $script:TuiFrameHeight; $row++) {
        $line = $script:TuiFrameLines[$row - 1]
        if ($null -eq $line) { $line = "".PadRight($script:TuiFrameWidth) }
        [void]$frame.Append("$([char]27)[$row;1H$line$([char]27)[0m")
    }
    # Keep the terminal cursor away from the bottom status row. Some child
    # processes/host paths can emit blank lines while refresh is running; if
    # the cursor is left on the last row those lines scroll the whole TUI up.
    [void]$frame.Append("$([char]27)[1;1H")
    [void]$frame.Append("$([char]27)[?2026l")
    $script:TuiLastFrameLines = [string[]]$script:TuiFrameLines.Clone()
    $script:TuiLastFrameHeight = $script:TuiFrameHeight
    $script:TuiLastFrameWidth = $script:TuiFrameWidth
    $script:TuiFrameLines = $null
    [Console]::Write($frame.ToString())
    Flush-Tui
}

function Enter-TuiScreen {
    $script:TuiLastFrameLines = $null
    $script:TuiLastFrameHeight = 0
    $script:TuiLastFrameWidth = 0
    Write-Ansi "$([char]27)[?1049h$([char]27)[?25l$([char]27)[2J$([char]27)[H"
    Flush-Tui
}

function Exit-TuiScreen {
    $script:TuiFrameBuffer = $null
    $script:TuiFrameLines = $null
    $script:TuiLastFrameLines = $null
    Write-Ansi "$([char]27)[0m$([char]27)[?25h$([char]27)[?1049l"
    Flush-Tui
}

function Write-TuiLine([int]$Row, [string]$Text, [int]$Width, [switch]$Reverse, [switch]$Bold) {
    if ($Row -lt 1 -or $Width -lt 1) { return }
    $value = if ($Text.Length -gt $Width) { $Text.Substring(0, $Width) } else { $Text.PadRight($Width) }
    $style = ""
    if ($Bold) { $style += "$([char]27)[1m" }
    if ($Reverse) { $style += "$([char]27)[7m" }
    if ($null -ne $script:TuiFrameLines) {
        if ($Row -gt $script:TuiFrameLines.Count) { return }
        $script:TuiFrameLines[$Row - 1] = "$style$value$([char]27)[0m"
        return
    }
    Write-Ansi "$([char]27)[$Row;1H$style$value$([char]27)[0m"
}

function Write-TuiPopup([int]$Row, [string]$Text, [int]$Width) {
    if ($Row -lt 1 -or $Width -lt 1) { return }
    $popup = " $Text "
    if ($popup.Length -gt $Width) { $popup = $popup.Substring(0, $Width) }
    $left = [Math]::Max(0, [int][Math]::Floor(($Width - $popup.Length) / 2))
    $right = [Math]::Max(0, $Width - $left - $popup.Length)
    $value = (" " * $left) + "$([char]27)[7m$popup$([char]27)[0m" + (" " * $right)
    if ($null -ne $script:TuiFrameLines) {
        if ($Row -gt $script:TuiFrameLines.Count) { return }
        $script:TuiFrameLines[$Row - 1] = $value
        return
    }
    Write-Ansi "$([char]27)[$Row;1H$value$([char]27)[0m"
}

function Read-TuiText([string]$Prompt, [switch]$Secret) {
    Exit-TuiScreen
    try {
        if ($Secret) {
            $secure = Read-Host $Prompt -AsSecureString
            $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        }
        return Read-Host $Prompt
    }
    finally {
        Enter-TuiScreen
    }
}

function Select-TuiOption([string]$Title, [string[]]$Labels) {
    $selected = 0
    while ($true) {
        $width = [Math]::Max(20, [Console]::WindowWidth - 1)
        $height = [Math]::Max(8, [Console]::WindowHeight)
        Begin-TuiFrame $height 4
        try {
            Write-TuiLine 1 $Title $width -Bold
            Write-TuiLine 2 "Enter selects. Up/Down or j/k moves. q/Esc cancels." $width
            Write-TuiLine 3 ("-" * $width) $width
            for ($i = 0; $i -lt $Labels.Count; $i++) {
                Write-TuiLine (4 + $i) ("$(if ($i -eq $selected) { '>' } else { ' ' }) $($Labels[$i])") $width -Reverse:($i -eq $selected)
            }
            Write-TuiLine $height " " $width
        }
        finally {
            End-TuiFrame
        }
        $key = [Console]::ReadKey($true)
        if ($key.Key -eq [ConsoleKey]::UpArrow -or $key.KeyChar -eq "k") {
            $selected = [Math]::Max(0, $selected - 1)
        }
        elseif ($key.Key -eq [ConsoleKey]::DownArrow -or $key.KeyChar -eq "j") {
            $selected = [Math]::Min($Labels.Count - 1, $selected + 1)
        }
        elseif ($key.Key -eq [ConsoleKey]::Enter) { return $selected }
        elseif ($key.Key -eq [ConsoleKey]::Escape -or $key.KeyChar -eq "q") { return -1 }
    }
}

function Invoke-NewContextWizard {
    $name = (Read-TuiText "New context name").Trim()
    if (-not $name) { return $null }
    Test-ContextName $name
    $labels = @(
        "ChatGPT browser login",
        "ChatGPT device-code login",
        "OpenAI API key (optional custom OpenAI base URL)",
        "Codex access token",
        "Custom provider API endpoint and key",
        "Custom provider using an environment variable"
    )
    $mode = Select-TuiOption "New Context: $name" $labels
    if ($mode -lt 0) { return $null }

    switch ($mode) {
        0 {
            Exit-TuiScreen
            try { Invoke-CodexLogin $name @("--use") }
            finally { Enter-TuiScreen }
        }
        1 {
            Exit-TuiScreen
            try { Invoke-CodexLogin $name @("--device-auth", "--use") }
            finally { Enter-TuiScreen }
        }
        2 {
            $secret = (Read-TuiText "OpenAI API key" -Secret).Trim()
            if (-not $secret) { throw "OpenAI API key cannot be empty" }
            $baseUrl = (Read-TuiText "OpenAI API base URL (blank for default)").Trim()
            $options = [System.Collections.Generic.List[string]]::new()
            $options.Add("--with-api-key"); $options.Add("--use")
            if ($baseUrl) { $options.Add("--base-url"); $options.Add($baseUrl) }
            Exit-TuiScreen
            try { Invoke-CodexLogin $name $options.ToArray() ($secret + "`n") }
            finally { Enter-TuiScreen }
        }
        3 {
            $secret = (Read-TuiText "Codex access token" -Secret).Trim()
            if (-not $secret) { throw "Codex access token cannot be empty" }
            Exit-TuiScreen
            try { Invoke-CodexLogin $name @("--with-access-token", "--use") ($secret + "`n") }
            finally { Enter-TuiScreen }
        }
        { $_ -in @(4, 5) } {
            $providerId = (Read-TuiText "Provider id (for example customapi)").Trim()
            Test-ProviderId $providerId
            $providerName = (Read-TuiText "Provider display name (optional)").Trim()
            $baseUrl = (Read-TuiText "Provider API base URL").Trim()
            if (-not $baseUrl) { throw "Provider API base URL is required" }
            $model = (Read-TuiText "Model (default gpt-5.5)").Trim()
            if (-not $model) { $model = "gpt-5.5" }
            $wireApi = (Read-TuiText "Wire API (default responses)").Trim()
            if (-not $wireApi) { $wireApi = "responses" }
            if ($mode -eq 4) {
                $secret = (Read-TuiText "Provider API key" -Secret).Trim()
                if (-not $secret) { throw "Provider API key cannot be empty" }
                New-ProviderContext $name $providerId $model $providerName $baseUrl $wireApi $secret "" -Overwrite
            } else {
                $envKey = (Read-TuiText "Environment variable containing the API key").Trim()
                if (-not $envKey) { throw "Environment variable name cannot be empty" }
                New-ProviderContext $name $providerId $model $providerName $baseUrl $wireApi "" $envKey -Overwrite
            }
            Use-Context $name
        }
    }
    return $name
}

function Show-Tui {
    if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        Get-Contexts | Format-Table -AutoSize
        Write-Host "non-interactive shell detected; use 'cdxsw use <name>' to activate a context."
        return
    }
    $selected = 0
    $top = 0
    $message = ""
    $lazyRateLimitAttempts = @{}
    $pendingRateLimitRefresh = $null
    $pendingActivation = $null
    Enter-TuiScreen
    try {
        while ($true) {
            $rows = @(Get-Contexts)
            if ($rows.Count -gt 0) {
                $selected = [Math]::Max(0, [Math]::Min($selected, $rows.Count - 1))
            } else {
                $selected = 0
            }
            $width = [Math]::Max(30, [Console]::WindowWidth - 1)
            $height = [Math]::Max(12, [Console]::WindowHeight)
            $detailLines = 8
            $listHeight = [Math]::Max(1, $height - $detailLines - 5)
            if ($selected -lt $top) { $top = $selected }
            if ($selected -ge $top + $listHeight) { $top = $selected - $listHeight + 1 }

            Begin-TuiFrame $height 5
            try {
                Write-TuiLine 1 "Codex Switcher" $width -Bold
                Write-TuiLine 2 "CODEX_HOME: $script:CodexHomePath" $width
                Write-TuiLine 3 "Enter: activate  Delete: delete  n: new  r: refresh  Up/Down or j/k: move  q/Esc: quit" $width
                Write-TuiLine 4 ("-" * $width) $width
                if ($rows.Count -eq 0) {
                    Write-TuiLine 5 "No saved contexts. Press n to create one." $width
                } else {
                    for ($offset = 0; $offset -lt $listHeight; $offset++) {
                        $index = $top + $offset
                        if ($index -ge $rows.Count) { break }
                        $row = $rows[$index]
                        $activeMarker = if ($row.Active) { "*" } else { " " }
                        $selectMarker = if ($index -eq $selected) { ">" } else { " " }
                        $line = "{0}{1} {2,-22} {3,-14} {4,-12} auth:{5,-3} 5h:{6,-10} wk:{7,-10}" -f `
                            $activeMarker, $selectMarker,
                            $row.Name, $row.Model, $row.Provider, $row.Auth, $row.FiveHour, $row.Weekly
                        Write-TuiLine (5 + $offset) $line $width -Reverse:($index -eq $selected)
                    }
                }
                $detailRow = 5 + $listHeight
                Write-TuiLine $detailRow ("-" * $width) $width
                if ($rows.Count -gt 0) {
                    $row = $rows[$selected]
                    Write-TuiLine ($detailRow + 1) "name:          $($row.Name)" $width -Bold
                    Write-TuiLine ($detailRow + 2) "model:         $($row.Model)" $width
                    Write-TuiLine ($detailRow + 3) "provider:      $($row.Provider)" $width
                    Write-TuiLine ($detailRow + 4) "auth:          $($row.Auth)" $width
                    Write-TuiLine ($detailRow + 5) "provider auth: $($row.ProviderAuth)" $width
                    Write-TuiLine ($detailRow + 6) "five-hour:     $($row.FiveHourDetail)" $width
                    Write-TuiLine ($detailRow + 7) "weekly:        $($row.WeeklyDetail)" $width
                }
                if ($message) {
                    Write-TuiPopup $height $message $width
                } else {
                    Write-TuiLine $height "Ready." $width -Bold
                }
            }
            finally {
                End-TuiFrame
            }

            if ($pendingActivation) {
                $activationName = $pendingActivation
                $pendingActivation = $null
                try {
                    Use-Context $activationName *> $null
                    $message = "Activated context '$activationName'. Restart Codex app to apply it."
                    if ($script:RestartRequested) { Restart-CodexApp *> $null }
                } catch {
                    $message = "error: $($_.Exception.Message)"
                }
                continue
            }

            if ($pendingRateLimitRefresh) {
                $refreshName = $pendingRateLimitRefresh
                $pendingRateLimitRefresh = $null
                try {
                    Update-ContextRateLimits $refreshName *> $null
                    $message = "Rate limits updated for '$refreshName'."
                } catch {
                    $message = "error: $($_.Exception.Message)"
                }
                continue
            }

            if ($rows.Count -gt 0) {
                $row = $rows[$selected]
                $needsLazyRateLimitFetch = (
                    $row.ProviderAuth -eq "codex-auth" -and
                    $row.Auth -eq "yes" -and
                    ($row.FiveHour -eq "?" -or $row.Weekly -eq "?") -and
                    -not $lazyRateLimitAttempts.ContainsKey($row.Name)
                )
                if ($needsLazyRateLimitFetch) {
                    $lazyRateLimitAttempts[$row.Name] = $true
                    $pendingRateLimitRefresh = $row.Name
                    $message = "Fetching rate limits for '$($row.Name)'..."
                    continue
                }
            }

            $key = [Console]::ReadKey($true)
            $message = ""
            if ($key.Key -eq [ConsoleKey]::UpArrow -or $key.KeyChar -eq "k") { $selected-- }
            elseif ($key.Key -eq [ConsoleKey]::DownArrow -or $key.KeyChar -eq "j") { $selected++ }
            elseif ($key.Key -eq [ConsoleKey]::PageDown -or $key.KeyChar -eq " ") { $selected += $listHeight }
            elseif ($key.Key -eq [ConsoleKey]::PageUp) { $selected -= $listHeight }
            elseif ($key.Key -eq [ConsoleKey]::Home -or $key.KeyChar -eq "g") { $selected = 0 }
            elseif ($key.Key -eq [ConsoleKey]::End -or $key.KeyChar -eq "G") { $selected = $rows.Count - 1 }
            elseif ($key.Key -eq [ConsoleKey]::Enter -and $rows.Count -gt 0) {
                $pendingActivation = $rows[$selected].Name
                $message = "Activating context '$($rows[$selected].Name)'..."
            }
            elseif ($key.KeyChar -eq "n") {
                try {
                    $created = Invoke-NewContextWizard
                    if ($created) {
                        $message = "Stored and activated context '$created'. Restart Codex app to apply it."
                        $rows = @(Get-Contexts)
                        for ($i = 0; $i -lt $rows.Count; $i++) {
                            if ($rows[$i].Name -eq $created) { $selected = $i; break }
                        }
                    }
                } catch { $message = "error: $($_.Exception.Message)" }
            }
            elseif ($key.Key -eq [ConsoleKey]::Delete -and $rows.Count -gt 0) {
                $answer = Select-TuiOption "Delete '$($rows[$selected].Name)'?" @(
                    "Cancel",
                    "Delete saved config/auth snapshot"
                )
                if ($answer -eq 1) {
                    $deleted = $rows[$selected].Name
                    Remove-Item -LiteralPath (Get-ContextDirectory $deleted -Require) -Recurse -Force
                    $message = "Deleted context '$deleted'."
                    $selected = [Math]::Max(0, $selected - 1)
                } else { $message = "Delete cancelled." }
            }
            elseif ($key.KeyChar -eq "r") {
                if ($rows.Count -eq 0) {
                    $message = "No context selected."
                } else {
                    $pendingRateLimitRefresh = $rows[$selected].Name
                    $message = "Fetching rate limits for '$($rows[$selected].Name)'..."
                }
            }
            elseif ($key.Key -eq [ConsoleKey]::Escape -or $key.KeyChar -eq "q") { return }
            else { $message = "Unknown key. Enter activates, n adds, Delete removes, q exits." }
        }
    }
    finally {
        Exit-TuiScreen
        [Console]::WriteLine()
    }
}

$CodexHome = Get-OptionValue $Arguments "--codex-home" $CodexHome
$Store = Get-OptionValue $Arguments "--store" $Store
$CodexBin = Get-OptionValue $Arguments "--codex-bin" $CodexBin
$script:CodexHomePath = Resolve-UserPath $CodexHome
$script:StorePath = Resolve-UserPath $Store
$script:CodexBinPath = $CodexBin
$script:ContextsDir = Join-Path $script:StorePath "contexts"
$script:BackupsDir = Join-Path $script:StorePath "backups"
$script:HomesDir = Join-Path $script:StorePath "homes"
$script:ActiveFile = Join-Path $script:StorePath "active.json"
$script:RestartRequested = $RestartApp -or (Test-Option $Arguments "--restart-app")
Ensure-Directory $script:ContextsDir
Ensure-Directory $script:BackupsDir
Ensure-Directory $script:HomesDir

$lockPath = Join-Path $script:StorePath ".lock"
$lock = $null
try {
    try {
        $lock = [System.IO.File]::Open($lockPath, "OpenOrCreate", "ReadWrite", "None")
    }
    catch {
        throw "another codexswitcher process is already running"
    }

    $positionals = @(Get-PositionalArguments $Arguments)
    switch ($Command.ToLowerInvariant()) {
        { $_ -in @("capture", "save") } {
            if ($positionals.Count -lt 1) { throw "capture requires a context name" }
            Capture-Context $positionals[0] -Overwrite:(Test-Option $Arguments "--overwrite")
            Write-Host "saved context '$($positionals[0])' from $script:CodexHomePath"
        }
        "use" {
            if ($positionals.Count -lt 1) { throw "use requires a context name" }
            Use-Context $positionals[0] -KeepAuth:(Test-Option $Arguments "--keep-auth")
            Write-Host "activated context '$($positionals[0])' in $script:CodexHomePath"
            if ($script:RestartRequested) { Restart-CodexApp }
            elseif (Get-Process -Name "Codex" -ErrorAction SilentlyContinue) {
                Write-Host "note: restart the Codex app to pick up auth/config changes (or pass --restart-app)."
            }
        }
        "list" {
            foreach ($row in @(Get-Contexts)) {
                if ($row.ProviderAuth -eq "codex-auth" -and $row.Auth -eq "yes") {
                    try { Update-ContextRateLimits $row.Name } catch {}
                }
            }
            Get-Contexts | Format-Table -AutoSize
        }
        "status" {
            $config = Join-Path $script:CodexHomePath "config.toml"
            Write-Host "CODEX_HOME: $script:CodexHomePath"
            Write-Host "switcher store: $script:StorePath"
            Write-Host "active context: $(Get-ActiveContext)"
            Write-Host "model: $(Get-TopLevelTomlValue $config 'model')"
            Write-Host "provider: $(Get-TopLevelTomlValue $config 'model_provider' 'openai')"
            Write-Host "auth.json: $(if (Test-Path -LiteralPath (Join-Path $script:CodexHomePath 'auth.json')) { 'present' } else { 'missing' })"
        }
        "login" {
            if ($positionals.Count -lt 1) { throw "login requires a context name" }
            $secretInput = $null
            if (Test-Option $Arguments "--with-api-key") {
                $apiKey = Get-OptionValue $Arguments "--api-key"
                if ($apiKey) { $secretInput = $apiKey + "`n" }
            }
            Invoke-CodexLogin $positionals[0] $Arguments $secretInput
            Write-Host "stored login credentials in context '$($positionals[0])'"
            if ((Test-Option $Arguments "--use") -and $script:RestartRequested) { Restart-CodexApp }
        }
        "provider" {
            if ($positionals.Count -lt 1) { throw "provider requires a context name" }
            $providerId = Get-OptionValue $Arguments "--provider-id"
            $model = Get-OptionValue $Arguments "--model"
            $baseUrl = Get-OptionValue $Arguments "--base-url"
            if (-not $providerId) { throw "provider requires --provider-id" }
            if (-not $model) { throw "provider requires --model" }
            New-ProviderContext `
                $positionals[0] `
                $providerId `
                $model `
                (Get-OptionValue $Arguments "--provider-name" "") `
                $baseUrl `
                (Get-OptionValue $Arguments "--wire-api" "responses") `
                (Get-OptionValue $Arguments "--api-key" "") `
                (Get-OptionValue $Arguments "--env-key" "") `
                -RequiresOpenAiAuth:(Test-Option $Arguments "--requires-openai-auth") `
                -Overwrite:(Test-Option $Arguments "--overwrite")
            Write-Host "saved provider context '$($positionals[0])'"
            if (Test-Option $Arguments "--use") {
                Use-Context $positionals[0]
                Write-Host "activated context '$($positionals[0])'"
                if ($script:RestartRequested) { Restart-CodexApp }
            }
        }
        "run" {
            $runItems = @(Remove-GlobalOptions $Arguments)
            if ($runItems.Count -lt 2) { throw "run requires a context name and command after --" }
            $name = $runItems[0]
            $context = Get-ContextDirectory $name -Require
            $isolatedHome = Join-Path $script:HomesDir $name
            Ensure-Directory $isolatedHome
            Copy-FileAtomic (Join-Path $context "config.toml") (Join-Path $isolatedHome "config.toml")
            $contextAuth = Join-Path $context "auth.json"
            if (Test-Path -LiteralPath $contextAuth) {
                Copy-FileAtomic $contextAuth (Join-Path $isolatedHome "auth.json")
            }
            $runCommand = $runItems[1]
            if ($runCommand -eq "codex") { $runCommand = Get-CodexCommand $script:CodexBinPath }
            $runArgs = [System.Collections.Generic.List[string]]::new()
            for ($i = 2; $i -lt $runItems.Count; $i++) {
                $runArgs.Add([string]$runItems[$i])
            }
            $oldHome = $env:CODEX_HOME
            try {
                $env:CODEX_HOME = $isolatedHome
                $global:LASTEXITCODE = 0
                & $runCommand @($runArgs.ToArray())
                $exitCode = $LASTEXITCODE
            }
            finally {
                $env:CODEX_HOME = $oldHome
            }
            if (Test-Path -LiteralPath (Join-Path $isolatedHome "auth.json")) {
                Copy-FileAtomic (Join-Path $isolatedHome "auth.json") $contextAuth
            }
            $global:LASTEXITCODE = $exitCode
            if ($env:CODEXSWITCHER_CMD_LAUNCHER) { exit $exitCode }
            return
        }
        { $_ -in @("tui", "") } { Show-Tui }
        default {
            throw "unsupported Windows command '$Command'. Supported: tui, capture, use, login, provider, run, list, status"
        }
    }
}
catch {
    [Console]::Error.WriteLine("error: $($_.Exception.Message)")
    $global:LASTEXITCODE = 2
    if ($env:CODEXSWITCHER_CMD_LAUNCHER) { exit 2 }
    return
}
finally {
    if ($lock) { $lock.Dispose() }
}
