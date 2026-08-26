#!/usr/bin/env python3
"""structure_json.py - Stage 3: JSON 结构化

解析 gdb_raw.txt，按 === SECTION === 标记分段提取结构化信息，生成 structured.json

用法: python3 structure_json.py <context.json>
  $1: context.json 路径（必填）

输出: <工作目录>/structured.json

退出码:
  0 = 成功
  1 = 参数错误
  2 = context.json 不存在
  3 = gdb_raw.txt 不存在或为空
"""

import json
import os
import re
import sys
from pathlib import Path


def read_context(context_path: str) -> dict:
    """读取 context.json"""
    with open(context_path, "r", encoding="utf-8") as f:
        return json.load(f)


def split_sections(raw_text: str) -> dict:
    """按 === SECTION === 标记分段

    返回 {section_name: section_text} 字典
    """
    sections = {}
    # 匹配 === SECTION_NAME === 开头的标记
    pattern = re.compile(r"^=== (\w+) ===\s*$", re.MULTILINE)
    matches = list(pattern.finditer(raw_text))

    for i, match in enumerate(matches):
        section_name = match.group(1)
        start = match.end()
        # 找下一个标记或文件末尾
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
        section_text = raw_text[start:end].strip()
        sections[section_name] = section_text

    return sections


def parse_signal(text: str) -> dict:
    """解析崩溃信号和地址

    GDB 输出中信号信息在 core-file 加载后、=== REGISTERS === 之前出现：
      Program terminated with signal SIGSEGV, Segmentation fault.
      #0  0x... in func () at file.c:10

    本函数既解析 SIGNAL 段（如果有），也解析整个 raw_text 开头的信号信息。
    """
    signal = None
    reason = None
    address = None

    # 从 "Program terminated with signal XXX, YYY." 提取
    m = re.search(r"Program terminated with signal (\w+),\s*([^.\n]+)", text)
    if m:
        signal = m.group(1)
        reason = m.group(2).strip()

    # 从 "#0  0x... in ..." 提取崩溃地址
    m = re.search(r"^#0\s+(0x[0-9a-fA-F]+)", text, re.MULTILINE)
    if m:
        address = m.group(1)

    return {"signal": signal, "reason": reason, "address": address}


def parse_registers(text: str) -> dict:
    """解析 REGISTERS 段

    GDB 输出示例:
      r0             0x4e4d74          0x4e4d74
      pc             0xb6e4ebfc        0xb6e4ebfc <__default_sa_restorer+44>
    """
    registers = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 格式: name value [描述]
        parts = line.split()
        if len(parts) >= 2 and parts[0].isalpha():
            reg_name = parts[0]
            reg_value = parts[1]
            if reg_value.startswith("0x") or re.match(r"^-?\d+$", reg_value):
                registers[reg_name] = reg_value
    return registers


def parse_threads(text: str) -> list:
    """解析 THREADS 段

    GDB 输出示例:
      Id   Target Id          Frame
      * 1    Thread 0x7f... "prog" 0x... in func () at file.c:10
    """
    threads = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Id") or line.startswith("---"):
            continue
        # 格式: [*] Id Target Id "name" Frame
        m = re.match(r"^(\*?)\s*(\d+)\s+Thread\s+(0x[0-9a-fA-F]+)\s+\"([^\"]*)\"\s*(.*)", line)
        if m:
            threads.append({
                "id": int(m.group(2)),
                "current": m.group(1) == "*",
                "tid": m.group(3),
                "name": m.group(4),
                "frame": m.group(5).strip()
            })
    return threads


def parse_bt_all(text: str) -> list:
    """解析 BT/BT_ALL 段，提取所有线程的回溯

    支持两种格式：
    1. 带线程头: Thread N (Thread 0x... "name"):
    2. 无线程头（单线程，bt full 的输出）: 直接是 #0 #1 #2...

    GDB 输出示例:
      Thread 1 (Thread 0x7f... "prog"):
      #0  0x... in func () at file.c:10
      #1  0x... in func2 () at file.c:20
    """
    threads = []
    current_thread = None
    current_frames = []

    # 帧正则: #N  0xADDR in FUNC (ARGS) at FILE:LINE
    frame_pattern = re.compile(
        r"^#(\d+)\s+(0x[0-9a-fA-F]+)\s+in\s+([^()]+)(?:\(([^)]*)\))?"
        r"(?:\s+at\s+(.+?):(\d+))?"
    )
    # 线程头正则: Thread N (Thread 0x... "name"):  或 Thread N (Thread 0x... (LWP N)):
    thread_header = re.compile(r"^Thread\s+(\d+)\s+\(Thread\s+(0x[0-9a-fA-F]+)\s+\((?:LWP\s+\d+|\"[^\"]*\")\):")

    for line in text.splitlines():
        line = line.strip()

        # 匹配线程头
        m = thread_header.match(line)
        if m:
            if current_thread is not None:
                threads.append({
                    "id": current_thread["id"],
                    "tid": current_thread["tid"],
                    "name": current_thread.get("name", ""),
                    "frames": current_frames
                })
            current_thread = {
                "id": int(m.group(1)),
                "tid": m.group(2),
                "name": ""
            }
            current_frames = []
            continue

        # 匹配帧
        m = frame_pattern.match(line)
        if m:
            frame = {
                "frame": int(m.group(1)),
                "addr": m.group(2),
                "func": m.group(3).strip(),
                "args": m.group(4) if m.group(4) else "",
                "file": m.group(5) if m.group(5) else None,
                "line": int(m.group(6)) if m.group(6) else None
            }
            current_frames.append(frame)

    # 最后一个线程
    if current_thread is not None:
        threads.append({
            "id": current_thread["id"],
            "tid": current_thread["tid"],
            "name": current_thread.get("name", ""),
            "frames": current_frames
        })

    # 如果没有线程头但有帧（bt full 的单线程输出），包装成单线程
    if not threads and current_frames:
        threads.append({
            "id": 0,
            "tid": "",
            "name": "",
            "frames": current_frames
        })

    return threads


def parse_sharedlib(text: str) -> list:
    """解析 SHAREDLIB 段

    GDB 输出示例:
      From        To          Syms Read   Shared Object Library
      0xb6e0c000  0xb6e60fff  Yes (*)     /path/to/libc.so.0
      0x004d4000  0x004d5fff  No          /path/to/davinci
    """
    libs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("From") or line.startswith("---"):
            continue

        # 格式: From To Syms Read Shared Object Library
        m = re.match(r"^(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(Yes|No)(\s+\([^)]+\))?\s+(.+)$", line)
        if m:
            libs.append({
                "from": m.group(1),
                "to": m.group(2),
                "symbols": m.group(3) == "Yes",
                "path": m.group(5).strip(),
                "name": os.path.basename(m.group(5).strip())
            })
    return libs


def parse_frame(text: str) -> dict:
    """解析 FRAME 段（info frame 输出）

    GDB 输出示例:
      Stack frame at 0x8eb8ab40:
       pc = 0xb5cbad14 in __GI_memcpy; saved pc = 0xb6711604
       compiled in /path/to/memcpy.c
       source language c.
       Arglist at 0x8eb8ab40: r0=0x7e62fa68 r1=0x94051008
       Locals at 0x8eb8ab40: r2=0x00004000
       Saved registers:
        lr at 0x8eb8ab3c, sp at 0x8eb8ab38
    """
    text = text.strip()
    if not text:
        return {}

    frame = {"raw": text}

    # 栈帧地址（两种格式：ARM "Stack frame at", x86 "Stack level 0, frame at"）
    m = re.search(r"frame at (0x[0-9a-fA-F]+):", text)
    if m:
        frame["stack_frame_addr"] = m.group(1)

    # pc/rip 和 saved pc/saved rip（兼容 ARM 和 x86）
    # ARM:  pc = 0x... in func; saved pc = 0x...
    # x86:  rip = 0x... in func (file:line); saved rip = 0x...
    m = re.search(r"(?:pc|rip) = (0x[0-9a-fA-F]+) in ([^(;\s]+)(?:\s*\([^)]*\))?\s*;\s*saved (?:pc|rip) = (0x[0-9a-fA-F]+)", text)
    if m:
        frame["pc"] = m.group(1)
        frame["func"] = m.group(2).strip()
        frame["saved_pc"] = m.group(3)
    else:
        # 备用：只匹配 pc/rip = ... in func（无 saved pc 的情况）
        m = re.search(r"(?:pc|rip) = (0x[0-9a-fA-F]+) in ([^(;\s]+)", text)
        if m:
            frame["pc"] = m.group(1)
            frame["func"] = m.group(2).strip()

    # 源文件（x86 格式：compiled in /path；或无）
    m = re.search(r"compiled in (.+)", text)
    if m:
        frame["compiled_in"] = m.group(1).strip()

    # 源语言
    m = re.search(r"source language (\w+\+*)", text)
    if m:
        frame["source_language"] = m.group(1)

    # saved registers
    saved_regs = {}
    m = re.search(r"Saved registers:\s*\n(.+)", text, re.DOTALL)
    if m:
        regs_text = m.group(1)
        for reg_match in re.finditer(r"(\w+) at (0x[0-9a-fA-F]+)", regs_text):
            saved_regs[reg_match.group(1)] = reg_match.group(2)
    if saved_regs:
        frame["saved_registers"] = saved_regs

    return frame


def parse_args(text: str) -> list:
    """解析 ARGS 段（info args 输出）

    GDB 输出示例:
      dst = 0x7e62fa68 ""
      src = 0x94051008
      n = 16384
    """
    args = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\w+)\s*=\s*(.+)$", line)
        if m:
            args.append({
                "name": m.group(1),
                "value": m.group(2).strip()
            })
    return args


def parse_locals(text: str) -> list:
    """解析 LOCALS 段（info locals 输出）

    格式同 args，可能含优化提示如 "<optimized out>"
    """
    locals_list = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\w+)\s*=\s*(.+)$", line)
        if m:
            locals_list.append({
                "name": m.group(1),
                "value": m.group(2).strip()
            })
    return locals_list


def parse_mappings(text: str) -> list:
    """解析 MAPPINGS 段（info proc mappings 输出）

    GDB 输出示例:
      0x00010000  0x011a5fff  0x011a6000  r-xp  davinci
      0x8eb8a000  0x8eb8c000  0x00002000  rw-p  [stack]
    """
    mappings = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Start") or line.startswith("---"):
            continue
        # 格式: start end size perms name
        m = re.match(
            r"^(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(\S+)\s*(.*)$",
            line
        )
        if m:
            mappings.append({
                "start": m.group(1),
                "end": m.group(2),
                "size": m.group(3),
                "perms": m.group(4),
                "name": m.group(5).strip() or None
            })
    return mappings


def parse_signal_info(text: str) -> dict:
    """解析 SIGNAL 段（info signals 输出，表格格式）

    GDB 输出示例:
      Signal        Stop	Print	Pass to program	Description
      SIGSEGV       Yes	Yes	Yes		Segmentation fault
      SIGILL        Yes	Yes	Yes		Illegal instruction
    """
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 跳过表头
        if line.startswith("Signal") and "Stop" in line:
            continue
        # 格式: SignalName Stop Print Pass Description
        # 用 split 分隔（制表符或空格）
        m = re.match(r"^(SIG\w+)\s+(Yes|No)\s+(Yes|No)\s+(Yes|No)\s+(.*)$", line)
        if m:
            result[m.group(1)] = {
                "stop": m.group(2) == "Yes",
                "print": m.group(3) == "Yes",
                "pass_to_program": m.group(4) == "Yes",
                "description": m.group(5).strip()
            }
    return result


def parse_disassembly(text: str) -> list:
    """解析 DISAS 段，提取反汇编行"""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        lines.append(line)
    return lines


def parse_source(text: str) -> list:
    """解析 SOURCE 段，提取源代码行"""
    lines = []
    for line in text.splitlines():
        lines.append(line)
    return lines


def main():
    import time
    stage3_start = time.time()

    if len(sys.argv) < 2:
        print("用法: python3 structure_json.py <context.json>", file=sys.stderr)
        sys.exit(1)

    context_path = sys.argv[1]
    if not os.path.isfile(context_path):
        print(f"错误: context.json 不存在: {context_path}", file=sys.stderr)
        sys.exit(2)

    context = read_context(context_path)
    work_dir = context["work_dir"]
    gdb_raw_path = os.path.join(work_dir, "gdb_raw.txt")

    if not os.path.isfile(gdb_raw_path) or os.path.getsize(gdb_raw_path) == 0:
        print(f"错误: gdb_raw.txt 不存在或为空: {gdb_raw_path}", file=sys.stderr)
        sys.exit(3)

    with open(gdb_raw_path, "r", encoding="utf-8", errors="replace") as f:
        raw_text = f.read()

    print(f"[Stage 3] 解析 gdb_raw.txt ({len(raw_text)} 字节)")

    # 分段
    sections = split_sections(raw_text)
    print(f"[Stage 3] 识别到 {len(sections)} 个段落: {list(sections.keys())}")

    # 解析各段
    # 信号信息在 raw_text 开头（core-file 加载后输出），不在 === SIGNAL === 段里
    structured = {
        "crash_info": parse_signal(raw_text),
        "registers": parse_registers(sections.get("REGISTERS", "")),
        "current_backtrace": parse_bt_all(sections.get("BT", "")),
        "crash_args": parse_args(sections.get("ARGS", "")),
        "crash_locals": parse_locals(sections.get("LOCALS", "")),
        "shared_libraries": parse_sharedlib(sections.get("SHAREDLIB", "")),
        "memory_mappings": parse_mappings(sections.get("MAPPINGS", "")),
        "threads": parse_threads(sections.get("THREADS", "")),
        "all_backtraces": parse_bt_all(sections.get("BT_ALL", "")),
        "disassembly": parse_disassembly(sections.get("DISAS", "")),
        "frame_info": parse_frame(sections.get("FRAME", "")),
        "signal_info": parse_signal_info(sections.get("SIGNAL", "")),
        "sections_found": list(sections.keys())
    }

    # 统计信息
    bt_thread_count = len(structured["all_backtraces"])
    bt_total_frames = sum(len(t["frames"]) for t in structured["all_backtraces"])
    lib_count = len(structured["shared_libraries"])
    libs_with_symbols = sum(1 for lib in structured["shared_libraries"] if lib["symbols"])

    print(f"[Stage 3] 线程数: {bt_thread_count}, 总帧数: {bt_total_frames}")
    print(f"[Stage 3] 共享库: {lib_count} (有符号: {libs_with_symbols})")

    # 写入 structured.json
    structured_path = os.path.join(work_dir, "structured.json")
    with open(structured_path, "w", encoding="utf-8") as f:
        json.dump(structured, f, indent=2, ensure_ascii=False)

    # 记录耗时到 context.json
    stage3_elapsed = time.time() - stage3_start
    context.setdefault("timing", {})["stage3_structure_json"] = stage3_elapsed
    with open(context_path, "w", encoding="utf-8") as f:
        json.dump(context, f, indent=2, ensure_ascii=False)

    print(f"[Stage 3] 完成: {structured_path} (耗时 {stage3_elapsed:.3f}s)")
    sys.exit(0)


if __name__ == "__main__":
    main()
