---
name: crash-analyze
description: 当用户提供 core dump 文件路径或 Crash Report URL 时触发。自动完成环境准备、GDB 取证、JSON 结构化、AI 分析，生成 Crash Report。用户说"分析这个 core 文件"或提供 .core/.dump 文件路径时触发；用户提供 http://.../view/<UUID> 格式的 Crash Report URL 时也触发。
allowed-tools: Bash, Read, Task, AskUserQuestion
---

当用户提供输入时，先判断输入类型，再走对应流程。

## 输入类型判断

- **输入是 URL**（匹配 `http://.../view/<UUID>`）→ 走 **Web Report 流程**（跳过 Stage 1-3）
- **输入是文件路径且存在**（.core/.dump）→ 走 **Core 文件流程**（现有 Stage 1-4）
- **无法判断** → 用 AskUserQuestion 询问用户是 URL 还是 core 文件

## 路径定位

**脚本目录通过以下方式定位（不写死路径）**：
- 优先用环境变量 `CRASH_SCRIPT_DIR`（crash 分析脚本）和 `RAG_SCRIPT_DIR`（RAG 脚本）
- 否则用 Bash 搜索：
  - `SCRIPT_DIR="$(find / -path '*/scripts/run_pipeline.sh' -type f 2>/dev/null | head -1 | xargs dirname 2>/dev/null)"`
  - `RAG_SCRIPT_DIR="$(find / -path '*/scripts/rag_search.py' -type f 2>/dev/null | head -1 | xargs dirname 2>/dev/null)"`
- 若都找不到，用 AskUserQuestion 询问用户脚本目录位置

---

## 流程 A：Web Report 流程（URL 输入）

适用于无 build-id、无 core 文件、仅有网页 Crash Report 的场景。

### 步骤 1：拉取并解析报告（1 次 Bash 调用）

```bash
python3 $SCRIPT_DIR/fetch_report.py <URL> [工作目录]
```

**根据退出码判断**：
- `0` = 成功，继续步骤 2
- `1` = 参数错误（URL 格式不对）
- `2` = URL 拉取失败（网络问题或服务不可达），告知用户并终止
- `3` = HTML 解析失败（页面结构不符），告知用户并终止

脚本输出末尾会打印崩溃信号、架构、设备信息、帧数、库数，直接读取即可。

`$WORK_DIR` 从输出中 `[Stage 0] 工作目录: xxx` 那行提取。

### 步骤 3：RAG 检索相似案例（1 次 Bash 调用）

在 AI 分析前，先检索知识库中的历史相似案例：

```bash
python3 $RAG_SCRIPT_DIR/rag_search.py --work-dir $WORK_DIR --action search
```

- `$RAG_SCRIPT_DIR` 定位方式同 `$SCRIPT_DIR`（优先环境变量 `RAG_SCRIPT_DIR`，否则搜索 `*/scripts/rag_search.py`）
- 脚本会调 RAG Server `/search` 接口，将相似案例保存到 `$WORK_DIR/rag_hits.json`
- 输出会打印命中条数和每条的 score/signature/signal/arch/version
- **如果 RAG Server 不可达**：打印提示后跳过，不影响后续分析（RAG 是增强，不是必需）

### 步骤 4：询问是否深入分析（1 次 AskUserQuestion 调用）

用 AskUserQuestion 询问用户：

> 是否有 ELF 文件或源码可供深入分析？
> - 选项 A：无，直接生成报告
> - 选项 B：有 ELF，提供路径继续深入

- **选 A** → 跳到步骤 5
- **选 B** → 用户提供 core 文件或 ELF 路径后，转走 **Core 文件流程**（Stage 1-3），用已有 structured.json 的模块地址信息辅助

### 步骤 5：AI 分析（1 次 Task 调用）

用 Task 工具调用 `crash-analyzer` Subagent：

```
Task(subagent_type="general-purpose", prompt="分析工作目录 $WORK_DIR 中的 Crash 数据，读取 structured.json 和 rag_hits.json，生成 crash_report.md。数据来源为 web_report，无 gdb_raw.txt。rag_hits.json 是 RAG 检索到的历史相似案例，参考其 analysis 和 solution 字段")
```

Subagent 会生成 `$WORK_DIR/crash_report.md`。

### 步骤 6：呈现结果（1 次 Read 调用）

读取 `$WORK_DIR/crash_report.md`，向用户展示报告内容。告知用户工作目录路径。

### 步骤 7：入库新案例（1 次 Bash 调用）

分析完成后，将新案例入库到 RAG 知识库（status=pending）：

```bash
python3 $RAG_SCRIPT_DIR/rag_search.py --work-dir $WORK_DIR --action ingest
```

- 脚本会调 RAG Server `/ingest` 接口，将 structured.json + crash_report.md 入库
- 同 source_url 重复入库会自动覆盖（以 source_url 的 MD5 作为 point id）
- **如果 RAG Server 不可达**：打印提示后跳过

---

## 流程 B：Core 文件流程（文件路径输入）

现有流程，保持不变。

### 步骤 1：运行 Stage 1-3（1 次 Bash 调用）

用 `run_pipeline.sh` 一次性完成环境准备 + GDB 取证 + JSON 结构化：

```bash
bash $SCRIPT_DIR/run_pipeline.sh <core文件>
```

**根据退出码判断**：
- `0` = 成功，继续步骤 2
- `2` = Stage 1 失败（core 文件问题或 debuginfod 未运行），告知用户并终止
- `3` = 需要用户输入（ARM 工具链未指定）：
  - 用 AskUserQuestion 询问用户选择 GDB 工具链
  - 用户选择后，将路径写入 `context.json` 的 `gdb_path` 字段
  - 重新运行 `run_pipeline.sh`
- `4` = Stage 2 失败（GDB 问题），告知用户并终止
- `5` = Stage 3 失败（解析问题），告知用户并终止

脚本输出末尾会打印崩溃信号、调用栈层数、栈顶函数等关键信息，直接读取即可，**不需要再 Read context.json 或 structured.json 验证**。

### 步骤 2：RAG 检索相似案例（1 次 Bash 调用）

在 AI 分析前，先检索知识库中的历史相似案例：

```bash
python3 $RAG_SCRIPT_DIR/rag_search.py --work-dir $WORK_DIR --action search
```

- `$RAG_SCRIPT_DIR` 定位方式同 `$SCRIPT_DIR`（优先环境变量 `RAG_SCRIPT_DIR`，否则搜索 `*/scripts/rag_search.py`）
- 脚本会调 RAG Server `/search` 接口，将相似案例保存到 `$WORK_DIR/rag_hits.json`
- **如果 RAG Server 不可达**：打印提示后跳过，不影响后续分析（RAG 是增强，不是必需）

### 步骤 3：AI 分析（1 次 Task 调用）

用 Task 工具调用 `crash-analyzer` Subagent：

```
Task(subagent_type="general-purpose", prompt="分析工作目录 $WORK_DIR 中的 Crash 数据，读取 structured.json、gdb_raw.txt 和 rag_hits.json，生成 crash_report.md。rag_hits.json 是 RAG 检索到的历史相似案例，参考其 analysis 和 solution 字段")
```

`$WORK_DIR` 从步骤 1 的输出中提取（`[Pipeline] 工作目录: xxx` 那行）。

Subagent 会生成 `$WORK_DIR/crash_report.md`。

### 步骤 4：呈现结果（1 次 Read 调用）

读取 `$WORK_DIR/crash_report.md`，向用户展示报告内容。告知用户工作目录路径。

### 步骤 5：入库新案例（1 次 Bash 调用）

分析完成后，将新案例入库到 RAG 知识库（status=pending）：

```bash
python3 $RAG_SCRIPT_DIR/rag_search.py --work-dir $WORK_DIR --action ingest
```

- 脚本会调 RAG Server `/ingest` 接口，将 structured.json + crash_report.md 入库
- **如果 RAG Server 不可达**：打印提示后跳过

## 关键设计：减少 LLM round-trip

- **合并 Stage 1-3**：用 `run_pipeline.sh` 一次 Bash 调用完成，避免 3 次脚本调用 + 3 次验证 = 6 次 LLM round-trip
- **不在主 Agent 验证中间产物**：脚本内部已做错误检查，退出码非 0 直接告知用户
- **关键信息从 stdout 提取**：脚本末尾打印关键信息，主 Agent 直接读输出，不需要再 Read JSON 文件

## 注意事项

- **不删除中间产物**：context.json、gdb_raw.txt、structured.json、report_raw.html 保留在工作目录
- **ARM 工具链询问**：只在 `run_pipeline.sh` 退出码 3 时询问，x86 自动用系统 gdb
- **路径不写死**：所有脚本路径通过 `SCRIPT_DIR` 变量定位
