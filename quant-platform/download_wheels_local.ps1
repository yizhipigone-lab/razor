# download_wheels_local.ps1 v3 - Pure Invoke-WebRequest, no Python/Docker needed
$Mirror = "http://pypi.tuna.tsinghua.edu.cn"
$WheelsDir = Join-Path $PSScriptRoot "wheels"
New-Item -ItemType Directory -Force -Path $WheelsDir | Out-Null

function Get-Wheel {
    param([string]$Pkg, [string]$Pattern)
    Write-Host "[dl] $Pkg ..." -NoNewline
    try {
        $html = (Invoke-WebRequest "$Mirror/simple/$Pkg/" -Headers @{"Accept"="text/html"} -UseBasicParsing).Content
        $rx = [regex]::Matches($html, 'href="([^"]*' + $Pattern + '[^"#]*\.whl)')
        $m = $rx | ForEach-Object { $_.Groups[1].Value } |
             Where-Object { $_ -notmatch "macosx|musllinux|win|i686|aarch64" }
        if (-not $m) { Write-Host " WARN: no match for $Pattern"; return }
        $href = ($m | Select-Object -Last 1) -replace '#.*',''
        $fname = Split-Path $href -Leaf
        $out = Join-Path $WheelsDir $fname
        if (Test-Path $out) { Write-Host " SKIP"; return }
        $url = "$Mirror/" + ($href -replace '^\.\./\.\./','')
        Invoke-WebRequest $url -OutFile $out -UseBasicParsing
        Write-Host " OK -> $fname"
    } catch { Write-Host " ERROR: $_" }
}

Write-Host "=== Phase 1: Large binary packages ==="
Get-Wheel "numpy"    "numpy-1\.26.*cp311.*manylinux.*x86_64"
Get-Wheel "pandas"   "pandas-2\.1.*cp311.*manylinux.*x86_64"
Get-Wheel "scipy"    "scipy-.*cp311.*manylinux.*x86_64"
Get-Wheel "polars"   "polars-.*abi3.*manylinux.*x86_64"
Get-Wheel "duckdb"   "duckdb-.*cp311.*manylinux.*x86_64"
Get-Wheel "numba"    "numba-.*cp311.*manylinux.*x86_64"
Get-Wheel "llvmlite" "llvmlite-.*cp311.*manylinux.*x86_64"

Write-Host ""
Write-Host "=== Phase 2: Remaining packages (direct + dependencies) ==="

# Binary packages
Get-Wheel "ta-lib"             "ta_lib-.*-cp311.*manylinux.*x86_64"
Get-Wheel "PyYAML"             "pyyaml-.*-cp311.*manylinux.*x86_64"
Get-Wheel "websockets"         "websockets-.*-cp311.*manylinux.*x86_64"
Get-Wheel "pydantic-core"      "pydantic_core-.*-cp311.*manylinux.*x86_64"
Get-Wheel "MarkupSafe"         "markupsafe-.*-cp311.*manylinux.*x86_64"
Get-Wheel "charset-normalizer" "charset_normalizer-.*-cp311.*manylinux.*x86_64"
Get-Wheel "orjson"             "orjson-.*-cp311.*manylinux.*x86_64"
Get-Wheel "greenlet"           "greenlet-.*-cp311.*manylinux.*x86_64"
Get-Wheel "SQLAlchemy"         "sqlalchemy-.*-cp311.*manylinux.*x86_64"

# Pure Python - direct requirements
Get-Wheel "vectorbt"          "vectorbt-.*-py3-none-any"
Get-Wheel "akshare"           "akshare-.*-py3-none-any"
Get-Wheel "loguru"            "loguru-.*-py3-none-any"
Get-Wheel "pydantic"          "pydantic-[0-9].*-py3-none-any"
Get-Wheel "tqdm"              "tqdm-.*-py3-none-any"
Get-Wheel "requests"          "requests-.*-py3-none-any"
Get-Wheel "pytdx2"            "pytdx2-.*-py3-none-any"
Get-Wheel "fastapi"           "fastapi-.*-py3-none-any"
Get-Wheel "uvicorn"           "uvicorn-.*-py3-none-any"
Get-Wheel "baostock"          "baostock-.*-py3-none-any"
Get-Wheel "jinja2"            "jinja2-.*-py3-none-any"
Get-Wheel "python-multipart"  "python_multipart-.*-py3-none-any"
Get-Wheel "python-dotenv"     "python_dotenv-.*-py3-none-any"
Get-Wheel "openai"            "openai-.*-py3-none-any"
Get-Wheel "tushare"           "tushare-.*-py3-none-any"
Get-Wheel "langgraph"         "langgraph-[0-9].*-py3-none-any"
Get-Wheel "langchain-core"    "langchain_core-.*-py3-none-any"
Get-Wheel "langchain-openai"  "langchain_openai-.*-py3-none-any"
Get-Wheel "optuna"            "optuna-.*-py3-none-any"

# Pure Python - known dependencies
Get-Wheel "annotated-types"   "annotated_types-.*-py3-none-any"
Get-Wheel "typing-extensions" "typing_extensions-.*-py3-none-any"
Get-Wheel "certifi"           "certifi-.*-py3-none-any"
Get-Wheel "idna"              "idna-.*-py3-none-any"
Get-Wheel "urllib3"           "urllib3-.*-py3-none-any"
Get-Wheel "starlette"         "starlette-.*-py3-none-any"
Get-Wheel "anyio"             "anyio-.*-py3-none-any"
Get-Wheel "sniffio"           "sniffio-.*-py3-none-any"
Get-Wheel "h11"               "h11-.*-py3-none-any"
Get-Wheel "click"             "click-.*-py3-none-any"
Get-Wheel "httpx"             "httpx-.*-py3-none-any"
Get-Wheel "httpcore"          "httpcore-.*-py3-none-any"
Get-Wheel "distro"            "distro-.*-py3-none-any"
Get-Wheel "packaging"         "packaging-.*-py3-none-any"
Get-Wheel "build"             "build-.*-py3-none-any"
Get-Wheel "pyproject-hooks"   "pyproject_hooks-.*-py3-none-any"
Get-Wheel "langsmith"         "langsmith-.*-py3-none-any"
Get-Wheel "tenacity"          "tenacity-.*-py3-none-any"
Get-Wheel "jsonpatch"         "jsonpatch-.*-py3-none-any"
Get-Wheel "jsonpointer"       "jsonpointer-.*-py3-none-any"
Get-Wheel "requests-toolbelt" "requests_toolbelt-.*-py3-none-any"
Get-Wheel "alembic"           "alembic-.*-py3-none-any"
Get-Wheel "Mako"              "mako-.*-py3-none-any"
Get-Wheel "colorlog"          "colorlog-.*-py3-none-any"
Get-Wheel "cmaes"             "cmaes-.*-py3-none-any"
Get-Wheel "python-dateutil"   "python_dateutil-.*-py3-none-any"
Get-Wheel "six"               "six-.*-py3-none-any"
Get-Wheel "apscheduler"       "apscheduler-3.*-py.*-none-any"
Get-Wheel "tzlocal"           "tzlocal-.*-py3-none-any"
Get-Wheel "pytz"              "pytz-.*-py2.py3-none-any"
Get-Wheel "tzdata"            "tzdata-.*-py2.py3-none-any"
Write-Host ""
$files = Get-ChildItem $WheelsDir
$totalMB = ($files | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("Total: {0} files, {1:N0} MB" -f $files.Count, $totalMB)
Write-Host "Done! Now run: docker-compose build"
