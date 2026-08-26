#!/usr/bin/env python3
"""fetch_report.py - 从 Crash Report URL 拉取并解析报告

从 web 报告页面拉取 HTML，提取设备信息和分析结果，生成 structured.json。
用于无 build-id、无 core 文件、仅有网页报告的场景。

用法: python3 fetch_report.py <URL> [工作目录]
  $1: 报告 URL（必填，格式 http://.../view/<UUID>）
  $2: 工作目录（可选，默认 /tmp/crash-analysis/<时间戳>）

输出:
  <工作目录>/report_raw.html    — 原始 HTML
  <工作目录>/structured.json     — 结构化数据
  <工作目录>/context.json        — 最小 context

退出码:
  0 = 成功
  1 = 参数错误
  2 = URL 拉取失败
  3 = HTML 解析失败（未找到关键内容）
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime


def fetch_html(url: str) -> str:
    """用 curl 拉取 HTML 内容"""
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "15", url],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode != 0:
            print(f"错误: curl 失败 (退出码 {result.returncode})", file=sys.stderr)
            sys.exit(2)
        html = result.stdout
        if not html or len(html) < 100:
            print(f"错误: 返回内容过短 ({len(html)} 字节)", file=sys.stderr)
            sys.exit(2)
        return html
    except subprocess.TimeoutExpired:
        print("错误: curl 超时", file=sys.stderr)
        sys.exit(2)


def extract_pre_blocks(html: str) -> tuple:
    """提取两个 <pre> 块：设备信息和分析结果

    返回 (device_info_text, report_text)
    """
    # <pre id="report-content">...</pre>
    report_match = re.search(
        r'<pre\s+id="report-content"[^>]*>(.*?)</pre>',
        html, re.DOTALL
    )
    if not report_match:
        print("错误: 未找到 <pre id=\"report-content\">", file=sys.stderr)
        sys.exit(3)
    report_text = report_match.group(1)

    # 第一个 <pre>（无 id）在 <h2>设备信息</h2> 之后
    device_match = re.search(
        r'<h2>设备信息</h2>\s*<pre[^>]*>(.*?)</pre>',
        html, re.DOTALL
    )
    device_info_text = device_match.group(1) if device_match else ""

    return device_info_text, report_text


def parse_device_info(text: str) -> dict:
    """解析设备信息块

    格式:
      ver: V5.11.0
      plat: h13
      prod: ...
      hikPipeID: ...
      minorType: ...
      crashpad_ver: v1.3.2
      ip: 10.65.150.227
    """
    info = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\w+):\s*(.+)$", line)
        if m:
            info[m.group(1)] = m.group(2).strip()
    return info


def parse_crash_reason(text: str) -> dict:
    """解析 Crash reason 和 Crash address

    格式:
      Crash reason:  SIGSEGV /SEGV_MAPERR
      Crash address: 0x94051008
    """
    result = {"signal": None, "reason": None, "address": None}

    m = re.search(r"^Crash reason:\s*(\S+)\s*(.*)$", text, re.MULTILINE)
    if m:
        result["signal"] = m.group(1)
        result["reason"] = m.group(2).strip() or None

    m = re.search(r"^Crash address:\s*(0x[0-9a-fA-F]+)", text, re.MULTILINE)
    if m:
        result["address"] = m.group(1)

    return result


def parse_os_and_cpu(text: str) -> dict:
    """解析 OS 和 CPU 信息，推断架构"""
    result = {"os_info": None, "cpu_info": None, "arch": "unknown"}

    # Operating system: Linux\n                  0.0.0 Linux 4.9.37 ... armv7l
    m = re.search(r"^Operating system:\s*(.+?)(?=^CPU:)", text, re.MULTILINE | re.DOTALL)
    if m:
        os_raw = m.group(1).strip()
        # 合并多行为单行
        os_raw = " ".join(os_raw.split())
        result["os_info"] = os_raw
        # 推断架构
        if "armv7l" in os_raw or "armv" in os_raw:
            result["arch"] = "arm"
        elif "aarch64" in os_raw:
            result["arch"] = "aarch64"
        elif "x86_64" in os_raw or "x64" in os_raw:
            result["arch"] = "x86_64"

    # CPU: arm\n     ARMv1 ARM part(0x4100c070) features: ...\n     2 CPUs
    m = re.search(r"^CPU:\s*(.+?)(?=^GPU:|^Crash)", text, re.MULTILINE | re.DOTALL)
    if m:
        cpu_raw = m.group(1).strip()
        result["cpu_info"] = " ".join(cpu_raw.split())

    return result


def parse_threads(text: str) -> list:
    """解析 Thread N (crashed) 块，提取帧和寄存器

    格式:
      Thread 0 (crashed)
       0  libc.so.6!__GI_memcpy + 0x194
          r0 = 0x7e62fa68    r1 = 0x94051008    r2 = 0x00004000
          ...
         Found by: given as instruction pointer in context
       1  libssl.so.1.1!WPACKET_memcpy [packet.c : 372 + 0xe]
          r4 = 0x00004000    ...
         Found by: call frame info
      ...
    """
    threads = []

    # 按 Thread N 分块
    thread_blocks = re.split(r"(?m)^Thread\s+(\d+)\s+\(([^)]+)\)\s*$", text)

    # split 后: [前缀, thread_id, thread_status, 块内容, thread_id, thread_status, 块内容, ...]
    i = 1
    while i < len(thread_blocks):
        thread_id = int(thread_blocks[i])
        thread_status = thread_blocks[i + 1]  # "crashed" 或 "LWP ...)"
        block = thread_blocks[i + 2] if i + 2 < len(thread_blocks) else ""

        frames = parse_thread_frames(block)
        threads.append({
            "id": thread_id,
            "crashed": "crashed" in thread_status,
            "frames": frames
        })
        i += 3

    return threads


def parse_thread_frames(block: str) -> list:
    """解析单个线程块内的帧"""
    frames = []
    current_frame = None

    # 帧头格式:
    #   N  module!func [file : line + offset]
    #   N  module!func + offset
    #   N  module + offset
    frame_pattern = re.compile(
        r"^\s*(\d+)\s+(\S+)!([^\s\[]+)"
        r"(?:\s*\[([^:]+?)\s*:\s*(\d+)\s*\+\s*(0x[0-9a-fA-F]+)\])?"
        r"(?:\s*\+\s*(0x[0-9a-fA-F]+))?"
        r"\s*$"
    )
    # 无符号帧: N  module + offset
    nosym_pattern = re.compile(
        r"^\s*(\d+)\s+(\S+)\s+\+\s*(0x[0-9a-fA-F]+)\s*$"
    )
    # 寄存器行: 每行可能有多个 "rN = 0x..." 用 finditer 全部提取
    reg_pattern = re.compile(r"(\w+)\s*=\s*(0x[0-9a-fA-F]+)")

    for line in block.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # 寄存器行（包含 = 0x 且不是帧头）
        if "=" in line_stripped and current_frame is not None and not line_stripped[0].isdigit():
            for m in reg_pattern.finditer(line_stripped):
                if "registers" not in current_frame:
                    current_frame["registers"] = {}
                current_frame["registers"][m.group(1)] = m.group(2)
            continue

        # "Found by:" 行 — 提取栈展开可信度
        if line_stripped.startswith("Found by:"):
            if current_frame is not None:
                found_by = line_stripped[len("Found by:"):].strip()
                current_frame["found_by"] = found_by
                # 可信度分级
                if "instruction pointer" in found_by:
                    current_frame["trust"] = "high"  # 崩溃帧 PC，最可信
                elif "call frame info" in found_by:
                    current_frame["trust"] = "high"  # CFI/DWARF 展开，可信
                elif "frame pointer" in found_by:
                    current_frame["trust"] = "medium"  # 帧指针链，中等
                elif "stack scanning" in found_by or "stack scan" in found_by:
                    current_frame["trust"] = "low"  # 栈扫描猜测，不可信
                else:
                    current_frame["trust"] = "unknown"
            continue

        # 帧头（有符号）
        m = frame_pattern.match(line_stripped)
        if m:
            if current_frame is not None:
                frames.append(current_frame)
            current_frame = {
                "frame": int(m.group(1)),
                "module": m.group(2),
                "func": m.group(3).strip(),
                "file": m.group(4).strip() if m.group(4) else None,
                "line": int(m.group(5)) if m.group(5) else None,
                "offset": m.group(6) or m.group(7) or None
            }
            continue

        # 帧头（无符号）
        m = nosym_pattern.match(line_stripped)
        if m:
            if current_frame is not None:
                frames.append(current_frame)
            current_frame = {
                "frame": int(m.group(1)),
                "module": m.group(2),
                "func": None,
                "file": None,
                "line": None,
                "offset": m.group(3)
            }
            continue

    if current_frame is not None:
        frames.append(current_frame)

    return frames


def parse_loaded_modules(text: str) -> list:
    """解析 Loaded modules 段

    格式:
      0x00010000 - 0x011a5fff  davinci  ???  (main)  (WARNING: Corrupt symbols, ...)
      0x8b911000 - 0x8b930fff  libsdk_admin.so.1  ???
    """
    libs = []
    in_modules = False

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("Loaded modules:"):
            in_modules = True
            continue
        if not in_modules:
            continue

        # 格式: 0xSTART - 0xEND  name  ???  [(main)] [(WARNING: ...)]
        m = re.match(
            r"^(0x[0-9a-fA-F]+)\s*-\s*(0x[0-9a-fA-F]+)\s+(\S+)(.*)$",
            line
        )
        if m:
            from_addr = m.group(1)
            to_addr = m.group(2)
            name = m.group(3)
            rest = m.group(4)

            is_main = "(main)" in rest
            # WARNING 标记或 ??? 都表示无符号
            has_warning = "WARNING:" in rest
            # breakpad 报告中 ??? 表示符号状态未知，通常无符号
            symbols = not has_warning and "???" not in rest.split("(main)")[0].strip() if is_main else not has_warning
            # 简化：有 WARNING 则 false，否则 true（breakpad ??? 不代表符号状态）
            symbols = not has_warning

            libs.append({
                "from": from_addr,
                "to": to_addr,
                "name": name,
                "symbols": symbols,
                "is_main": is_main,
                "warning": re.search(r"WARNING:\s*([^)]+)", rest).group(1).strip() if has_warning else None
            })

    return libs


def main():
    if len(sys.argv) < 2:
        print("用法: python3 fetch_report.py <URL> [工作目录]", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    if not url.startswith("http"):
        print(f"错误: 无效 URL: {url}", file=sys.stderr)
        sys.exit(1)

    work_dir = sys.argv[2] if len(sys.argv) >= 3 else f"/tmp/crash-analysis/{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(work_dir, exist_ok=True)

    stage0_start = time.time()

    print(f"[Stage 0] URL: {url}")
    print(f"[Stage 0] 工作目录: {work_dir}")

    # 1. 拉取 HTML
    print("[Stage 0] 拉取 HTML...")
    html = fetch_html(url)
    html_path = os.path.join(work_dir, "report_raw.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[Stage 0] 已保存原始 HTML: {html_path} ({len(html)} 字节)")

    # 2. 提取 <pre> 块
    print("[Stage 0] 解析 HTML...")
    device_info_text, report_text = extract_pre_blocks(html)

    # 3. 解析各部分
    device_info = parse_device_info(device_info_text)
    crash_info = parse_crash_reason(report_text)
    os_cpu = parse_os_and_cpu(report_text)
    threads = parse_threads(report_text)
    shared_libraries = parse_loaded_modules(report_text)

    # 4. 生成 structured.json
    structured = {
        "crash_info": crash_info,
        "device_info": device_info,
        "os_info": os_cpu["os_info"],
        "cpu_info": os_cpu["cpu_info"],
        "arch": os_cpu["arch"],
        "current_backtrace": threads,
        "all_backtraces": threads,
        "shared_libraries": shared_libraries,
        "source": "web_report",
        "source_url": url
    }

    structured_path = os.path.join(work_dir, "structured.json")
    with open(structured_path, "w", encoding="utf-8") as f:
        json.dump(structured, f, indent=2, ensure_ascii=False)

    # 5. 生成最小 context.json
    context = {
        "source_url": url,
        "work_dir": work_dir,
        "arch": os_cpu["arch"],
        "device_info": device_info,
        "source": "web_report",
        "timing": {"stage0_fetch_report": time.time() - stage0_start}
    }
    context_path = os.path.join(work_dir, "context.json")
    with open(context_path, "w", encoding="utf-8") as f:
        json.dump(context, f, indent=2, ensure_ascii=False)

    # 6. 汇总输出
    elapsed = time.time() - stage0_start
    crashed_thread = next((t for t in threads if t.get("crashed")), threads[0] if threads else None)
    frame_count = len(crashed_thread["frames"]) if crashed_thread else 0

    print(f"[Stage 0] 完成 (耗时 {elapsed:.3f}s)")
    print(f"[Stage 0] 工作目录: {work_dir}")
    print(f"[Stage 0] 产物: report_raw.html、structured.json、context.json")
    print(f"[Stage 0] 崩溃信号: {crash_info.get('signal', '未知')}")
    print(f"[Stage 0] 崩溃地址: {crash_info.get('address', '未知')}")
    print(f"[Stage 0] 架构: {os_cpu['arch']}")
    print(f"[Stage 0] 设备: {device_info.get('plat', '未知')} / {device_info.get('ver', '未知')}")
    print(f"[Stage 0] 线程数: {len(threads)}, 崩溃线程帧数: {frame_count}")
    print(f"[Stage 0] 共享库: {len(shared_libraries)} 个")

    sys.exit(0)


if __name__ == "__main__":
    main()
