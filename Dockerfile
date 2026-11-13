FROM python:3.11-slim

# 不写 .pyc、日志直出，便于容器排障
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DICOM_REMAP_DATA=/data \
    DICOM_REMAP_MAX_FILES=20 \
    DICOM_REMAP_MAX_TOTAL_BYTES=16777216

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用、测试与演示脚本
COPY app /app/app
COPY tests /app/tests
COPY scripts /app/scripts

# 数据卷：SQLite、暂存与已发布 ZIP 均在 /data 下，重启保留已发布结果
RUN mkdir -p /data/staging /data/published
VOLUME ["/data"]

EXPOSE 8000

# 入口：建库/启动恢复 -> 生成样例（可选）-> 启动服务
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
