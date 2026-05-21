# Setup pre-commit hooks for bot-observability (Windows)
# Run this once after cloning: .\setup-hooks.ps1

$HooksDir = ".\.git\hooks"
$HookFile = "$HooksDir\pre-commit"

# Make sure hooks directory exists
if (!(Test-Path $HooksDir)) {
    New-Item -ItemType Directory -Path $HooksDir -Force | Out-Null
}

# Create the pre-commit hook for Windows (batch wrapper around jq)
$HookContent = @'
@echo off
REM Pre-commit hook: Validate dashboard JSON files

echo Validating dashboard JSON files...

REM Get list of JSON files being committed in grafana/provisioning/dashboards/json/
setlocal enabledelayedexpansion
set errors=0

for /f "delims=" %%f in ('git diff --cached --name-only --diff-filter=ACM ^| findstr "grafana\\provisioning\\dashboards\\json.*\.json"') do (
    if exist "%%f" (
        echo Checking %%f...
        
        REM Check JSON syntax
        jq empty "%%f" >nul 2>&1
        if errorlevel 1 (
            echo ERROR: Invalid JSON in %%f
            jq empty "%%f"
            set /a errors+=1
            goto next_file
        )
        
        REM Check required Grafana fields
        jq -e ".title and .panels and .schemaVersion" "%%f" >nul 2>&1
        if errorlevel 1 (
            echo ERROR: Missing required Grafana dashboard fields in %%f
            echo   Required: title, panels, schemaVersion
            set /a errors+=1
            goto next_file
        )
        
        REM Check all panels have required fields
        jq -e ".panels | all(.type and .id and .gridPos)" "%%f" >nul 2>&1
        if errorlevel 1 (
            echo ERROR: Panel(s) in %%f missing required fields ^(type, id, gridPos^)
            set /a errors+=1
        )
        
        :next_file
    )
)

if !errors! gtr 0 (
    echo.
    echo X %errors% dashboard JSON file(s) have errors. Fix before committing.
    exit /b 1
)

echo All dashboard JSON files are valid.
exit /b 0
'@

Set-Content -Path $HookFile -Value $HookContent -Force
Write-Host "✓ Pre-commit hook installed at $HookFile" -ForegroundColor Green
Write-Host ""
Write-Host "Next time you commit, dashboard JSON files will be automatically validated."
Write-Host "To skip validation (not recommended): git commit --no-verify"
