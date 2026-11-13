#!/usr/bin/env bash
# 容器/本地一键验收：生成样例 -> 跑单元测试 -> 跑端到端演示（跨批次引用 + 同源并发）。
# 在容器内：docker compose exec dicom-remap scripts/verify.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> [1/3] 生成合成 DICOM 样例"
python -I scripts/generate_samples.py "${DICOM_REMAP_DATA:-/data}/samples"

echo "==> [2/3] 运行 pytest 单元/集成测试"
python -I -m pytest tests/ -q

echo "==> [3/3] 运行端到端演示（含跨批次引用、同源并发、项目隔离、失败回滚）"
python -I scripts/demo.py --samples "${DICOM_REMAP_DATA:-/data}/samples"

echo "==> 验收全部通过"
