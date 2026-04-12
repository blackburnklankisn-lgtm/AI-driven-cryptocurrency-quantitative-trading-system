FROM python:3.9-slim

WORKDIR /app

# 安装系统依赖（pyarrow 等需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖配置，先安装依赖（利用 Docker cache 层）
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e "." || pip install --no-cache-dir \
    pandas polars numpy ccxt pydantic "pydantic-settings>=2.0" \
    python-dotenv pyyaml loguru scikit-learn pyarrow prometheus-client

# 复制源代码
COPY core/ ./core/
COPY modules/ ./modules/
COPY apps/ ./apps/
COPY configs/ ./configs/

# 创建必要目录
RUN mkdir -p storage logs

# 健康检查（Prometheus 指标端口）
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/metrics || exit 1

EXPOSE 8000

CMD ["python", "-m", "apps.trader.main"]
