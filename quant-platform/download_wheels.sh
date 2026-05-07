#!/bin/bash
# 绕过 WSL2/Docker 中 pip JSON 解析内存腐败 Segfault
# wget 使用 C 层 I/O，无 Python 堆操作，完全安全
set -e

MIRROR="http://pypi.tuna.tsinghua.edu.cn"
WDIR="/wheels"
mkdir -p "$WDIR"

dl() {
    local pkg="$1" pat="$2"
    echo "[dl] $pkg"
    wget -q --header="Accept: text/html" -O /tmp/_idx "${MIRROR}/simple/${pkg}/" || \
        { echo "  WARN: index fetch failed for $pkg"; return 0; }
    local href
    href=$(grep -oE "href=\"[^\"]*${pat}[^\"#]*\.whl" /tmp/_idx \
           | grep -iv macos | grep -iv musl | grep -iv win | grep -iv i686 | grep -iv aarch64 \
           | sort -V | tail -1 | sed 's/href="//;s/".*//' | sed 's/#.*//')
    if [ -z "$href" ]; then
        echo "  WARN: no match for $pkg (${pat})"
        return 0
    fi
    local fname url
    fname=$(basename "$href")
    url="${MIRROR}/${href#../../}"
    echo "  -> $fname"
    wget -q "$url" -O "${WDIR}/${fname}" && echo "  OK"
}

echo "=== 下载大型二进制轮子（触发pip内存腐败的包）==="

# 科学计算核心
dl numpy         "numpy-1[.]26[^\"]*cp311[^\"]*manylinux[^\"]*x86_64"
dl pandas        "pandas-2[.]1[^\"]*cp311[^\"]*manylinux[^\"]*x86_64"
dl scipy         "scipy-[^\"]*cp311[^\"]*manylinux[^\"]*x86_64"
# polars 使用 cp38-abi3（稳定 ABI），不含 cp311 字样
dl polars        "polars-[^\"]*abi3[^\"]*manylinux[^\"]*x86_64"
dl duckdb        "duckdb-[^\"]*cp311[^\"]*manylinux[^\"]*x86_64"

# vectorbt 的重量级依赖
dl llvmlite      "llvmlite-[^\"]*cp311[^\"]*manylinux[^\"]*x86_64"
dl numba         "numba-[^\"]*cp311[^\"]*manylinux[^\"]*x86_64"

# pandas 的纯 Python 依赖（指定最低版本避免拉到上古版本）
dl six              "six-1[.]1[^\"]*py2[.]py3-none"
dl pytz             "pytz-202[^\"]*py2[.]py3-none"
dl tzdata           "tzdata-202[^\"]*py2[.]py3-none"
dl python-dateutil  "python_dateutil-2[.]8[^\"]*py2[.]py3-none"

rm -f /tmp/_idx
echo ""
echo "=== 已下载的轮子 ==="
ls -lh "$WDIR"
echo ""

# 先安装核心包（无网络，从 /wheels 读取）
echo "=== 离线安装核心包 ==="
python -m pip install --no-index --find-links="$WDIR" \
    "numpy>=1.26.0" "pandas>=2.1.0" scipy "polars>=0.20.0" "duckdb>=0.10.0"

echo "=== 核心包离线安装完成（/wheels 保留供后续步骤使用）==="
