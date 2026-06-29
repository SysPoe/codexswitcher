[CmdletBinding()]
param(
    [string]$BinDirectory = (Join-Path $HOME ".local\bin"),
    [switch]$Copy
)

$ErrorActionPreference = "Stop"
$sourceDirectory = $PSScriptRoot
$targetScript = Join-Path $sourceDirectory "codexswitcher.py"
$targetPowerShell = Join-Path $sourceDirectory "codexswitcher.ps1"
$targetCmd = Join-Path $sourceDirectory "codexswitcher.cmd"

if (-not (Test-Path -LiteralPath $targetScript)) {
    throw "codexswitcher.py was not found beside this installer."
}
if (-not (Test-Path -LiteralPath $targetPowerShell)) {
    throw "codexswitcher.ps1 was not found beside this installer."
}
if (-not (Test-Path -LiteralPath $targetCmd)) {
    throw "codexswitcher.cmd was not found beside this installer."
}

[void][System.IO.Directory]::CreateDirectory($BinDirectory)

function Install-LinkOrCopy([string]$Destination, [string]$Source) {
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Force
    }
    if (-not $Copy -and [System.IO.Path]::GetFileName($Destination) -ne "cdxsw") {
        try {
            New-Item -ItemType SymbolicLink -Path $Destination -Target $Source -ErrorAction Stop | Out-Null
            return
        }
        catch {
            Write-Verbose "Symlink creation failed for $Destination; installing a launcher instead."
        }
    }

    switch ([System.IO.Path]::GetExtension($Destination).ToLowerInvariant()) {
        ".ps1" {
            $content = "& '$($Source.Replace("'", "''"))' @args`nexit `$LASTEXITCODE`n"
            [System.IO.File]::WriteAllText($Destination, $content, [System.Text.UTF8Encoding]::new($false))
        }
        ".cmd" {
            $escaped = $Source.Replace("%", "%%")
            if ([System.IO.Path]::GetExtension($Source) -eq ".cmd") {
                $content = "@echo off`r`ncall `"$escaped`" %*`r`nexit /b %ERRORLEVEL%`r`n"
            }
            else {
                $content = @"
@echo off
setlocal
set "CODEXSWITCHER_CMD_LAUNCHER=1"
pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$escaped" %*
set "CODEXSWITCHER_EXIT=%ERRORLEVEL%"
endlocal & exit /b %CODEXSWITCHER_EXIT%
"@ -replace "`n", "`r`n"
            }
            [System.IO.File]::WriteAllText($Destination, $content, [System.Text.ASCIIEncoding]::new())
        }
        default {
            $unixSource = $Source.Replace("\", "/").Replace("'", "'\''")
            $content = "#!/usr/bin/env sh`nSCRIPT='$unixSource'`nif command -v python3 >/dev/null 2>&1; then exec python3 `"`$SCRIPT`" `"`$@`"; fi`nexec python `"`$SCRIPT`" `"`$@`"`n"
            [System.IO.File]::WriteAllText($Destination, $content, [System.Text.UTF8Encoding]::new($false))
        }
    }
}

Install-LinkOrCopy (Join-Path $BinDirectory "cdxsw.ps1") $targetPowerShell
Install-LinkOrCopy (Join-Path $BinDirectory "cdxsw.cmd") $targetCmd
Install-LinkOrCopy (Join-Path $BinDirectory "cdxsw") $targetScript

Write-Host "Installed cdxsw launchers in $BinDirectory"
Write-Host "Run: cdxsw status"
