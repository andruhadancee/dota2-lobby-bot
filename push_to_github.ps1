# Usage:
#   Right-click this file and "Run with PowerShell"
#   or run from terminal:
#   powershell -ExecutionPolicy Bypass -File .\push_to_github.ps1
#   Optional: powershell -ExecutionPolicy Bypass -File .\push_to_github.ps1 -RepoUrl "https://github.com/andruhadancee/dota2-lobby-bot.git"

param(
    [string]$RepoUrl = "https://github.com/andruhadancee/dota2-lobby-bot.git"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host "[ OK ] $msg" -ForegroundColor Green }

# Move to script directory to avoid running from the wrong place
$scriptDir = Split-Path -Path $MyInvocation.MyCommand.Path -Parent
Set-Location -Path $scriptDir
Write-Info "Working directory: $((Get-Location).Path)"

# Guard: do not run from user home directory
$userHome = [Environment]::GetFolderPath('UserProfile')
if ((Get-Location).Path -ieq $userHome) {
    throw "Script started from home directory ($userHome). Move this file into the project folder and run again."
}

# Initialize git repo if missing
if (-not (Test-Path .git)) {
    Write-Info "git init"
    git init | Out-Null
}

# Ensure basic .gitignore exists
$gitIgnorePath = ".gitignore"
$gitIgnoreDesired = @(
    "node_modules/",
    ".env",
    ".env.*",
    "dist/",
    "build/",
    ".vscode/",
    ".DS_Store",
    "*.log"
)

if (-not (Test-Path $gitIgnorePath)) {
    Write-Info "Creating .gitignore"
    $gitIgnoreDesired -join [Environment]::NewLine | Out-File -Encoding utf8 $gitIgnorePath
} else {
    $existing = try { Get-Content -Raw -ErrorAction Stop $gitIgnorePath } catch { "" }
    foreach ($line in $gitIgnoreDesired) {
        if ($existing -notmatch [regex]::Escape($line)) {
            Add-Content -Encoding utf8 $gitIgnorePath $line
        }
    }
}

# Configure remote origin
try {
    $null = git remote get-url origin 2>$null
    Write-Info "Removing existing origin"
    git remote remove origin | Out-Null
} catch {}

Write-Info "Setting origin: $RepoUrl"
git remote add origin $RepoUrl

# Ensure main branch
Write-Info "Switching to main"
git checkout -B main | Out-Null

# Stage all and commit if needed
Write-Info "Staging files"
git add -A

git diff --cached --quiet
$exit = $LASTEXITCODE
if ($exit -ne 0) {
    Write-Info "Committing"
    git commit -m "Initial import" | Out-Null
} else {
    Write-Warn "No changes to commit"
}

# Push (force to ensure repo reflects local state)
Write-Info "Pushing to origin/main (force)"
git push -u origin main --force
Write-Ok "Done. Check repository: $RepoUrl"


