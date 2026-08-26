#!/bin/bash
# run_pipeline.sh - 合并 Stage 1-3 为单次调用
# 依次执行：prepare_env.sh → gdb_batch.sh → structure_json.py
# 主 Agent 只需 1 次 Bash 调用即可完成 Stage 1-3，减少 LLM round-trip
#
# 用法: run_pipeline.sh <core文件> [工作目录]
#   $1: core 文件路径（必填）
#   $2: 工作目录（可选，默认 /tmp/crash-analysis/<时间戳>）
#
# 退出码:
#   0 = 全部成功
#   1 = 参数错误
#   2 = Stage 1 失败（环境准备）
#   3 = Stage 1 需要用户输入（ARM 工具链未指定）
#   4 = Stage 2 失败（GDB batch）
#   5 = Stage 3 失败（JSON 结构化）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ===== 参数处理 =====
CORE_FILE="${1:-}"
if [ -z "$CORE_FILE" ]; then
    echo "用法: $0 <core文件> [工作目录]" >&2
    exit 1
fi

WORK_DIR="${2:-/tmp/crash-analysis/$(date +%Y%m%d_%H%M%S)}"
PIPELINE_START=$(date +%s.%N)

echo "=========================================="
echo "[Pipeline] core 文件: $CORE_FILE"
echo "[Pipeline] 工作目录: $WORK_DIR"
echo "=========================================="

# ===== Stage 1：环境准备 =====
echo ""
echo "[Pipeline] >>> Stage 1: 环境准备"
if ! bash "$SCRIPT_DIR/prepare_env.sh" "$CORE_FILE" "$WORK_DIR"; then
    echo "[Pipeline] Stage 1 失败" >&2
    exit 2
fi

# 检查是否需要用户输入
NEEDS_USER_INPUT=$(python3 -c "import json; print(json.load(open('$WORK_DIR/context.json')).get('needs_user_input', False))" 2>/dev/null || echo "False")
if [ "$NEEDS_USER_INPUT" = "True" ]; then
    echo "[Pipeline] Stage 1 完成，但需要用户指定 GDB 工具链"
    echo "[Pipeline] 请主 Agent 用 AskUserQuestion 询问用户，然后重新运行本脚本"
    exit 3
fi

# ===== Stage 2：GDB Batch 取证 =====
echo ""
echo "[Pipeline] >>> Stage 2: GDB Batch 取证"
if ! bash "$SCRIPT_DIR/gdb_batch.sh" "$WORK_DIR/context.json"; then
    echo "[Pipeline] Stage 2 失败" >&2
    exit 4
fi

# ===== Stage 3：JSON 结构化 =====
echo ""
echo "[Pipeline] >>> Stage 3: JSON 结构化"
if ! python3 "$SCRIPT_DIR/structure_json.py" "$WORK_DIR/context.json"; then
    echo "[Pipeline] Stage 3 失败" >&2
    exit 5
fi

# ===== 汇总 =====
PIPELINE_END=$(date +%s.%N)
PIPELINE_ELAPSED=$(echo "$PIPELINE_END - $PIPELINE_START" | bc 2>/dev/null || echo "?")

echo ""
echo "=========================================="
echo "[Pipeline] Stage 1-3 全部完成 (总耗时 ${PIPELINE_ELAPSED}s)"
echo "[Pipeline] 工作目录: $WORK_DIR"
echo "[Pipeline] 产物: context.json, gdb_raw.txt, structured.json"
echo "=========================================="

# 输出 structured.json 的关键字段供主 Agent 快速判断
python3 -c "
import json
s = json.load(open('$WORK_DIR/structured.json'))
ci = s.get('crash_info', {})
print(f'[Pipeline] 崩溃信号: {ci.get(\"signal\", \"未知\")}')
print(f'[Pipeline] 崩溃地址: {ci.get(\"address\", \"未知\")}')
bt = s.get('current_backtrace', [])
if bt:
    frames = bt[0].get('frames', [])
    print(f'[Pipeline] 调用栈层数: {len(frames)}')
    if frames:
        top = frames[0]
        print(f'[Pipeline] 栈顶: {top.get(\"func\", \"??\")} at {top.get(\"file\", \"??\")}:{top.get(\"line\", \"??\")}')
libs = s.get('shared_libraries', [])
print(f'[Pipeline] 共享库: {len(libs)} 个')
"
