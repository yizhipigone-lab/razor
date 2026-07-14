<#
.SYNOPSIS
  Stop quant-platform Python processes by command-line match.
.DESCRIPTION
  Fallback killer invoked by stop.bat. Catches processes that the port-based
  killer (8888 / 8001) misses:
    - Batch backtest scripts (e.g. rerun_hmqb_hs.py) that open NO port
    - Stuck uvicorn workers that stopped LISTENING but still hold DuckDB
  Match features (any one):
    venv313              project venv (strongest signal)
    quant-platform       project root path
    main.py              API service
    uvicorn/live_trader  live trader service
    rerun_hmqb           backtest script family
    scripts/backtest     backtest script dir
    scripts/run_         runner scripts
  Unrelated python.exe (e.g. bpm_approver.py, server.py from other projects)
  are NOT matched because they use D:\...\Python313 and lack these features.
  No python/psutil dependency - pure Windows PowerShell + taskkill.
#>

$ErrorActionPreference = 'SilentlyContinue'

$patterns = @(
    'venv313',
    'quant-platform',
    'main\.py',
    'uvicorn.*live_trader',
    'app\.live_trader',
    'rerun_hmqb',
    'scripts[\\/]backtest',
    'scripts[\\/]run_'
)
$regex = ($patterns -join '|')

Write-Host ""
Write-Host "[3/5] Fallback: stop project python.exe by command-line match..."
$procs = @(Get-CimInstance Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -match $regex })

if ($procs.Count -eq 0) {
    Write-Host "  [--] No matching project python process"
} else {
    foreach ($p in $procs) {
        $cmd = $p.CommandLine
        if ($cmd.Length -gt 90) { $cmd = $cmd.Substring(0, 90) + '...' }
        Write-Host ("  -> kill PID={0}  {1}" -f $p.ProcessId, $cmd)
        # /T = kill child process tree (handles uvicorn reload parent/child, cmd->python)
        & taskkill /F /T /PID $p.ProcessId 2>&1 | Out-Null
    }
    Write-Host ("  [OK] Processed {0} process(es) + child tree" -f $procs.Count)
}

# ---- verify ----
Write-Host ""
Write-Host "[verify] Remaining project python processes:"
$remain = @(Get-CimInstance Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -match $regex })
if ($remain.Count -eq 0) {
    Write-Host "  [OK] Clean - no project python process left"
} else {
    Write-Host "  [WARN] Still alive (need manual check):"
    foreach ($p in $remain) {
        $cmd = $p.CommandLine
        if ($cmd.Length -gt 90) { $cmd = $cmd.Substring(0, 90) + '...' }
        Write-Host ("    PID={0}  {1}" -f $p.ProcessId, $cmd)
    }
}
