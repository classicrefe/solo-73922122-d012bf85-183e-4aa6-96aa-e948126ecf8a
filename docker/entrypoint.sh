#!/usr/bin/env bash
# 容器入口：按需生成演示样例，然后启动 uvicorn。
# 建表、种子项目与中断恢复由应用 lifespan 启动钩子统一完成（见 app/main.py）。
set -euo pipefail

mkdir -p "${DICOM_REMAP_DATA}/staging" "${DICOM_REMAP_DATA}/published"

# 首次启动生成合成 DICOM 样例（挂载卷已存在样例时跳过）
if [[ "${GENERATE_SAMPLES:-true}" == "true" && ! -d "${DICOM_REMAP_DATA}/samples/batch_a" ]]; then
  python -I scripts/generate_samples.py "${DICOM_REMAP_DATA}/samples"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
