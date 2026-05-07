# 第一阶段：编译依赖（TA-Lib 等)
FROM docker.m.daocloud.io/library/python:3.11-slim-bookworm AS builder

# 使用国内镜像源加速
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
    sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources

# 安装编译工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential wget gcc make autoconf automake libtool \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 编译安装 TA-Lib C 库
RUN wget -q http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    cp /usr/share/misc/config.guess . && \
    cp /usr/share/misc/config.sub . && \
    ./configure --prefix=/usr && \
    make && make install && \
    cd .. && rm -rf ta-lib*

COPY wheels /build/wheels
COPY requirements.txt /build/requirements.txt

RUN pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn && \
    pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --find-links=/build/wheels \
        numpy pandas duckdb vectorbt python-dateutil pydantic pydantic-core && \
    TA_LIBRARY_PATH=/usr/lib TA_INCLUDE_PATH=/usr/include \
    pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --find-links=/build/wheels \
        /build/wheels/ta_lib-0.6.8-cp311-cp311-manylinux*.whl && \
    pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --require-hashes=false -r /build/requirements.txt

# 第二阶段：运行环境
FROM docker.m.daocloud.io/library/python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 拷贝 TA-Lib C 库及 Python 环境
COPY --from=builder /usr/lib/libta_lib.so* /usr/lib/
COPY --from=builder /usr/lib/libta_lib.a /usr/lib/
COPY --from=builder /usr/include/ta-lib /usr/include/ta-lib
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app
COPY . .

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
