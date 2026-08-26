#!/bin/bash
# gdb_batch.sh - Stage 2: GDB Batch 取证
# 读 context.json，用 GDB batch 模式收集取证信息
#
# 用法: gdb_batch.sh <context.json> [gdb命令文件]
#   $1: context.json 路径（必填）
#   $2: GDB 命令文件路径（可选，默认 scripts/gdb_cmds.txt）
#
# 前提: context.json 的 gdb_path 和 sysroot 必须已就绪（needs_user_input=false）
#
# 输出: <工作目录>/gdb_raw.txt
#
# 退出码:
#   0 = 成功
#   1 = 参数错误
#   2 = context.json 不存在
#   3 = gdb_path 未设置（需要用户先指定）
#   4 = GDB 命令文件不存在
#   5 = GDB 执行失败

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ===== 参数处理 =====
CONTEXT_JSON="${1:-}"
if [ -z "$CONTEXT_JSON" ]; then
    echo "用法: $0 <context.json> [gdb命令文件]" >&2
    exit 1
fi

if [ ! -f "$CONTEXT_JSON" ]; then
    echo "错误: context.json 不存在: $CONTEXT_JSON" >&2
    exit 2
fi

GDB_CMDS_FILE="${2:-$SCRIPT_DIR/gdb_cmds.txt}"
if [ ! -f "$GDB_CMDS_FILE" ]; then
    echo "错误: GDB 命令文件不存在: $GDB_CMDS_FILE" >&2
    echo "请创建 gdb_cmds.txt 或指定路径" >&2
    exit 4
fi

# ===== 从 context.json 读取字段 =====
read_field() {
    python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$CONTEXT_JSON" "$1"
}

# 计时开始
STAGE2_START=$(date +%s.%N)

WORK_DIR=$(read_field "work_dir")
GDB_PATH=$(read_field "gdb_path")
SYSROOT=$(read_field "sysroot")
SOLIB_SEARCH_PATH=$(read_field "solib_search_path")
MAIN_ELF_PATH=$(read_field "main_elf_path")
CORE_FILE=$(read_field "core_file")
NEEDS_USER_INPUT=$(read_field "needs_user_input")

if [ "$NEEDS_USER_INPUT" = "True" ] || [ -z "$GDB_PATH" ]; then
    echo "错误: gdb_path 未设置，需要用户先指定工具链" >&2
    echo "请通过环境变量 CRASH_GDB_PATH 或编辑 context.json 的 gdb_path 字段" >&2
    exit 3
fi

if [ ! -x "$GDB_PATH" ]; then
    echo "错误: GDB 不可执行: $GDB_PATH" >&2
    exit 3
fi

echo "[Stage 2] GDB: $GDB_PATH"
echo "[Stage 2] core: $CORE_FILE"
echo "[Stage 2] main ELF: $MAIN_ELF_PATH"
echo "[Stage 2] sysroot: ${SYSROOT:-（无）}"
echo "[Stage 2] solib-search-path: $SOLIB_SEARCH_PATH"
echo "[Stage 2] 命令文件: $GDB_CMDS_FILE"

# ===== 生成实际 GDB 命令文件（替换占位符）=====
ACTUAL_CMDS_FILE="$WORK_DIR/gdb_cmds_actual.txt"

python3 -c "
import json, sys

context = json.load(open(sys.argv[1]))
template = open(sys.argv[2]).read()

replacements = {
    '{{SYSROOT}}': context.get('sysroot') or '',
    '{{SOLIB_SEARCH_PATH}}': context.get('solib_search_path') or '',
    '{{MAIN_ELF_PATH}}': context.get('main_elf_path') or '',
    '{{CORE_FILE}}': context.get('core_file') or '',
}

result = template
for placeholder, value in replacements.items():
    result = result.replace(placeholder, value)

with open(sys.argv[3], 'w') as f:
    f.write(result)
print('[Stage 2] 已生成实际命令文件: ' + sys.argv[3])
" "$CONTEXT_JSON" "$GDB_CMDS_FILE" "$ACTUAL_CMDS_FILE"

# ===== 运行 GDB batch =====
GDB_RAW="$WORK_DIR/gdb_raw.txt"
echo "[Stage 2] 运行 GDB batch..."

# 设置 GDB 环境变量
GDB_ENV=""
if [ -n "$SYSROOT" ]; then
    # sysroot 在命令文件里通过 set sysroot 设置，这里不重复
    true
fi

# 执行 GDB
# --batch: 批处理模式，执行完命令文件后退出
# -x: 指定命令文件
set +e
timeout 60 "$GDB_PATH" --batch -x "$ACTUAL_CMDS_FILE" > "$GDB_RAW" 2>&1
GDB_EXIT_CODE=$?
set -e

# 将 GDB 退出码和耗时写入 context.json
python3 -c "
import json, sys
ctx = json.load(open(sys.argv[1]))
ctx['gdb_exit_code'] = int(sys.argv[2])
ctx.setdefault('timing', {})['stage2_gdb_batch'] = float(sys.argv[4]) - float(sys.argv[3])
with open(sys.argv[1], 'w') as f:
    json.dump(ctx, f, indent=2, ensure_ascii=False)
" "$CONTEXT_JSON" "$GDB_EXIT_CODE" "$STAGE2_START" "$(date +%s.%N)"

if [ $GDB_EXIT_CODE -ne 0 ]; then
    echo "[Stage 2] 警告: GDB 退出码 $GDB_EXIT_CODE（部分命令可能未成功执行）" >&2
fi

# 检查输出非空
if [ ! -s "$GDB_RAW" ]; then
    echo "[Stage 2] 错误: GDB 输出为空" >&2
    exit 5
fi

LINE_COUNT=$(wc -l < "$GDB_RAW")
STAGE2_ELAPSED=$(echo "$(date +%s.%N) - $STAGE2_START" | bc 2>/dev/null || echo "?")
echo "[Stage 2] 完成: $GDB_RAW ($LINE_COUNT 行, 耗时 ${STAGE2_ELAPSED}s)"