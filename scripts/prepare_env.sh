#!/bin/bash
# prepare_env.sh - Stage 1: 环境准备
# 从 core 提取 build-id，从 debuginfod 下载 ELF，生成 context.json
#
# 用法: prepare_env.sh <core文件> [工作目录]
#   $1: core 文件路径（必填）
#   $2: 工作目录（可选，默认 /tmp/crash-analysis/<时间戳>）
#
# 输出: <工作目录>/context.json
#
# 退出码:
#   0 = 成功
#   1 = 参数错误
#   2 = core 文件不存在
#   3 = build-id 提取失败
#   4 = 主程序 ELF 下载失败

set -euo pipefail

# ===== 常量 =====
DEBUGINFOD_URL="${DEBUGINFOD_URL:-http://localhost:8002}"
CACHE_DIR="${HOME}/.cache/debuginfod"
# 改造版 elfutils 路径（临时，未来打包在一起后消除）
# 优先级：环境变量 ELFUTILS_DIR > 脚本同目录的 elfutils 子目录 > 默认路径
if [ -n "${ELFUTILS_DIR:-}" ]; then
    : # 用户指定
elif [ -d "$(dirname "${BASH_SOURCE[0]}")/elfutils" ]; then
    ELFUTILS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/elfutils" && pwd)"
else
    ELFUTILS_DIR="/home/xujialiu/debuginfod-test/elfutils-elfutils-0.186/elfutils-elfutils-0.186"
fi
UNSTRIP_BIN="${ELFUTILS_DIR}/src/unstrip"
READELF_BIN="${ELFUTILS_DIR}/src/readelf"
ELFUTILS_LIBS="${ELFUTILS_DIR}/libdw:${ELFUTILS_DIR}/libelf"

# ===== 参数处理 =====
CORE_FILE="${1:-}"
if [ -z "$CORE_FILE" ]; then
    echo "用法: $0 <core文件> [工作目录]" >&2
    exit 1
fi

CORE_FILE="$(readlink -f "$CORE_FILE")"
if [ ! -f "$CORE_FILE" ]; then
    echo "错误: core 文件不存在: $CORE_FILE" >&2
    exit 2
fi

WORK_DIR="${2:-/tmp/crash-analysis/$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$WORK_DIR"

# 计时开始
STAGE1_START=$(date +%s.%N)

echo "[Stage 1] 工作目录: $WORK_DIR"
echo "[Stage 1] core 文件: $CORE_FILE"
echo "[Stage 1] debuginfod: $DEBUGINFOD_URL"

# ===== 架构检测 =====
detect_arch() {
    local machine_line
    machine_line=$(LD_LIBRARY_PATH="$ELFUTILS_LIBS" "$READELF_BIN" -h "$CORE_FILE" 2>/dev/null \
        | grep "Machine:" || echo "")
    # readelf 输出: "Machine: Advanced Micro Devices X86-64" 或 "Machine: ARM"
    case "$machine_line" in
        *ARM*)       echo "arm" ;;
        *AArch64*)   echo "aarch64" ;;
        *X86-64*|*x86-64*|*x64*) echo "x86_64" ;;
        *)           echo "unknown" ;;
    esac
}

ARCH=$(detect_arch)
echo "[Stage 1] 架构: $ARCH"

# ===== GDB 路径检测（分层选择） =====
detect_gdb() {
    local gdb_path=""

    # 优先级 1: 环境变量 CRASH_GDB_PATH
    if [ -n "${CRASH_GDB_PATH:-}" ] && [ -x "$CRASH_GDB_PATH" ]; then
        gdb_path="$CRASH_GDB_PATH"
        echo "[Stage 1] GDB (来自 CRASH_GDB_PATH): $gdb_path" >&2
    fi

    # 优先级 2: context.json 已有 gdb_path（由调用方预先写入）
    if [ -z "$gdb_path" ] && [ -f "$WORK_DIR/context.json" ]; then
        local cached
        cached=$(python3 -c "import json; print(json.load(open('$WORK_DIR/context.json')).get('gdb_path',''))" 2>/dev/null || echo "")
        if [ -n "$cached" ] && [ -x "$cached" ]; then
            gdb_path="$cached"
            echo "[Stage 1] GDB (来自缓存): $gdb_path" >&2
        fi
    fi

    # 优先级 3: 架构判断
    if [ -z "$gdb_path" ]; then
        case "$ARCH" in
            x86_64)
                gdb_path=$(which gdb 2>/dev/null || echo "")
                if [ -n "$gdb_path" ]; then
                    echo "[Stage 1] GDB (x86 系统 gdb): $gdb_path" >&2
                fi
                ;;
            arm|aarch64)
                # ARM: 不自动猜工具链，标记需要用户输入
                echo "[Stage 1] GDB: ARM 架构，需要用户指定工具链" >&2
                gdb_path=""
                ;;
            *)
                gdb_path=$(which gdb 2>/dev/null || echo "")
                ;;
        esac
    fi

    echo "$gdb_path"
}

GDB_PATH=$(detect_gdb)

# ===== sysroot 推断 =====
detect_sysroot() {
    local gdb_path="$1"
    [ -z "$gdb_path" ] && echo "" && return

    local gdb_dir gdb_basename cross_gcc sysroot
    gdb_dir=$(dirname "$gdb_path")
    gdb_basename=$(basename "$gdb_path")
    cross_gcc="${gdb_dir}/${gdb_basename%-gdb}-gcc"

    if [ -f "$cross_gcc" ] && [ -x "$cross_gcc" ]; then
        sysroot=$("$cross_gcc" -print-sysroot 2>/dev/null || echo "")
        if [ -n "$sysroot" ] && [ -d "$sysroot" ]; then
            echo "[Stage 1] sysroot: $sysroot (来自 $cross_gcc)" >&2
            echo "$sysroot"
            return
        fi
    fi
    echo "[Stage 1] sysroot: 推断失败，需要用户指定" >&2
    echo ""
}

SYSROOT=""
if [ -n "$GDB_PATH" ]; then
    SYSROOT=$(detect_sysroot "$GDB_PATH")
fi

# ===== 提取 build-id 并下载 ELF =====
echo "[Stage 1] 提取 build-id..."

# eu-unstrip 输出格式: 地址+长度 build-id@偏移 - - 文件名
UNSTRIP_OUTPUT=$(LD_LIBRARY_PATH="$ELFUTILS_LIBS" "$UNSTRIP_BIN" -n --core="$CORE_FILE" 2>/dev/null || echo "")

if [ -z "$UNSTRIP_OUTPUT" ]; then
    echo "[Stage 1] 错误: eu-unstrip 未识别到模块" >&2
    exit 3
fi

# 主程序 build-id（第一个）
MAIN_BUILD_ID=$(echo "$UNSTRIP_OUTPUT" | grep -oP '^\S+\s+\K[0-9a-f]+(?=@)' | head -1 || echo "")

if [ -z "$MAIN_BUILD_ID" ]; then
    echo "[Stage 1] 错误: 未提取到主程序 build-id" >&2
    exit 3
fi

echo "[Stage 1] 主程序 build-id: $MAIN_BUILD_ID"

# 下载主程序 ELF（日志输出到 stderr，路径输出到 stdout）
download_elf() {
    local build_id="$1"
    local name="$2"
    local target_dir="$CACHE_DIR/$build_id"
    local target_file

    if [ "$name" = "executable" ] || [ -z "$name" ] || [ "$name" = "." ] || [ "$name" = "-" ]; then
        target_file="$target_dir/executable"
    else
        target_file="$target_dir/$(basename "$name")"
    fi

    mkdir -p "$target_dir"

    if [ -f "$target_file" ] && [ -s "$target_file" ]; then
        echo "[Stage 1] 已缓存: $(basename "$target_file") ($build_id)" >&2
    else
        local http_code
        http_code=$(curl -s -w "%{http_code}" -o "$target_file" \
            "$DEBUGINFOD_URL/buildid/$build_id/executable" 2>/dev/null || echo "000")
        if [ "$http_code" = "200" ] && [ -s "$target_file" ]; then
            echo "[Stage 1] 下载成功: $(basename "$target_file") ($build_id)" >&2
        else
            echo "[Stage 1] 下载失败: $(basename "$target_file") ($build_id) HTTP=$http_code" >&2
            rm -f "$target_file"
            return 1
        fi
    fi
    echo "$target_file"
}

MAIN_ELF_PATH=$(download_elf "$MAIN_BUILD_ID" "executable" || echo "")
if [ -z "$MAIN_ELF_PATH" ]; then
    echo "[Stage 1] 错误: 主程序 ELF 下载失败" >&2
    exit 4
fi

# ===== 下载动态库并收集 solib-search-path =====
echo "[Stage 1] 处理动态库..."

SOLIB_PATHS_FILE="$WORK_DIR/.solib_paths"
> "$SOLIB_PATHS_FILE"
echo "$CACHE_DIR/$MAIN_BUILD_ID" >> "$SOLIB_PATHS_FILE"

# 用临时文件收集 modules，避免子 shell 变量丢失
MODULES_FILE="$WORK_DIR/.modules.jsonl"
> "$MODULES_FILE"

# 主程序加入 modules
echo "{\"build_id\":\"$MAIN_BUILD_ID\",\"name\":\"$(basename "$CORE_FILE" | sed 's/\.[^.]*$//')\",\"start_addr\":\"\",\"elf_path\":\"$MAIN_ELF_PATH\",\"downloaded\":true,\"status\":\"main\"}" >> "$MODULES_FILE"

# 遍历所有模块（用 < <(...) 避免子 shell）
while IFS= read -r line; do
    [ -z "$line" ] && continue

    lib_build_id=$(echo "$line" | grep -oP '\b[0-9a-f]{32,}(?=@)' | head -1 || echo "")
    lib_name=$(echo "$line" | awk '{print $NF}')
    lib_start=$(echo "$line" | awk '{print $1}')

    [ -z "$lib_build_id" ] && continue
    [ -z "$lib_name" ] && continue

    lib_basename=$(basename "$lib_name")
    [ "$lib_basename" = "." ] && continue
    [ "$lib_basename" = "-" ] && continue
    [ "$lib_build_id" = "$MAIN_BUILD_ID" ] && continue

    local_elf_path=$(download_elf "$lib_build_id" "$lib_basename" || echo "")

    if [ -n "$local_elf_path" ] && [ -f "$local_elf_path" ]; then
        echo "$CACHE_DIR/$lib_build_id" >> "$SOLIB_PATHS_FILE"
        echo "{\"build_id\":\"$lib_build_id\",\"name\":\"$lib_basename\",\"start_addr\":\"$lib_start\",\"elf_path\":\"$local_elf_path\",\"downloaded\":true,\"status\":\"ok\"}" >> "$MODULES_FILE"
    else
        echo "{\"build_id\":\"$lib_build_id\",\"name\":\"$lib_basename\",\"start_addr\":\"$lib_start\",\"elf_path\":\"\",\"downloaded\":false,\"status\":\"download_failed\"}" >> "$MODULES_FILE"
    fi
done < <(echo "$UNSTRIP_OUTPUT")

# 拼接 solib-search-path
SOLIB_PATHS=$(sort -u "$SOLIB_PATHS_FILE" | tr '\n' ':' | sed 's/:$//')
rm -f "$SOLIB_PATHS_FILE"

echo "[Stage 1] solib-search-path: $SOLIB_PATHS"

# ===== 判断是否需要用户输入 =====
# 只有 GDB 未找到时才需要询问；sysroot 为空不算（x86 本地调试不需要 sysroot）
NEEDS_USER_INPUT="false"
[ -z "$GDB_PATH" ] && NEEDS_USER_INPUT="true"

# ===== 生成 context.json =====
echo "[Stage 1] 生成 context.json..."

# 用 Python 读取 JSONL 并生成最终 context.json
python3 -c "
import json, sys, os

modules = []
jsonl_path = sys.argv[11]
if os.path.isfile(jsonl_path):
    with open(jsonl_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                modules.append(json.loads(line))

context = {
    'core_file': sys.argv[1],
    'work_dir': sys.argv[2],
    'arch': sys.argv[3],
    'debuginfod_url': sys.argv[4],
    'gdb_path': sys.argv[5] if sys.argv[5] else None,
    'sysroot': sys.argv[6] if sys.argv[6] else None,
    'solib_search_path': sys.argv[7],
    'main_build_id': sys.argv[8],
    'main_elf_path': sys.argv[9],
    'needs_user_input': sys.argv[10] == 'true',
    'source_minidump': None,
    'modules': modules,
    'timing': {'stage1_prepare_env': float(sys.argv[13]) - float(sys.argv[12])}
}

with open(sys.argv[2] + '/context.json', 'w') as f:
    json.dump(context, f, indent=2, ensure_ascii=False)
print('[Stage 1] context.json 已生成: ' + sys.argv[2] + '/context.json')
" "$CORE_FILE" "$WORK_DIR" "$ARCH" "$DEBUGINFOD_URL" "$GDB_PATH" "$SYSROOT" "$SOLIB_PATHS" "$MAIN_BUILD_ID" "$MAIN_ELF_PATH" "$NEEDS_USER_INPUT" "$MODULES_FILE" "$STAGE1_START" "$(date +%s.%N)"

rm -f "$MODULES_FILE"
STAGE1_ELAPSED=$(echo "$(date +%s.%N) - $STAGE1_START" | bc 2>/dev/null || echo "?")
echo "[Stage 1] 完成 (耗时 ${STAGE1_ELAPSED}s)"
exit 0
