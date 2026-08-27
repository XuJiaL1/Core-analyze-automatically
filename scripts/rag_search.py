#!/usr/bin/env python3
"""rag_search.py - RAG 检索 + 入库辅助脚本

用法:
  # 检索相似案例（保存 rag_hits.json 到工作目录）
  python3 rag_search.py --work-dir /tmp/crash-analysis/xxx --action search

  # 入库新案例（status=pending）
  python3 rag_search.py --work-dir /tmp/crash-analysis/xxx --action ingest

  # 检索 + 入库（一步完成）
  python3 rag_search.py --work-dir /tmp/crash-analysis/xxx --action both

读取工作目录下的 structured.json，调 RAG Server API。
"""

import argparse
import json
import os
import sys
import urllib.request


def load_structured(work_dir: str) -> dict:
    p = os.path.join(work_dir, "structured.json")
    if not os.path.exists(p):
        print(f"ERROR: {p} 不存在", file=sys.stderr)
        sys.exit(1)
    with open(p) as f:
        return json.load(f)


def call_api(api_url: str, endpoint: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        f"{api_url}{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def do_search(api_url: str, data: dict, top_k: int = 5, score_threshold: float = 0.85) -> dict:
    """调 /search 检索相似案例"""
    payload = {
        "structured_json": data,
        "top_k": top_k,
        "score_threshold": score_threshold,
        "status": None,  # 冷启动期不过滤
    }
    result = call_api(api_url, "/search", payload)
    return result


def do_ingest(api_url: str, data: dict, analysis_path: str = None) -> dict:
    """调 /ingest 入库新案例（status=pending）"""
    analysis = None
    if analysis_path and os.path.exists(analysis_path):
        with open(analysis_path) as f:
            analysis = f.read()
    payload = {
        "structured_json": data,
        "status": "pending",
        "analysis": analysis,
        "solution": None,
    }
    return call_api(api_url, "/ingest", payload)


def main():
    parser = argparse.ArgumentParser(description="RAG 检索 + 入库辅助脚本")
    parser.add_argument("--work-dir", required=True, help="Crash 分析工作目录")
    parser.add_argument(
        "--action",
        choices=["search", "ingest", "both"],
        default="both",
        help="search=只检索, ingest=只入库, both=检索+入库",
    )
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--score-threshold", type=float, default=0.85)
    args = parser.parse_args()

    data = load_structured(args.work_dir)

    # 检索
    if args.action in ("search", "both"):
        print(f"[RAG] 检索相似案例 (top_k={args.top_k}, threshold={args.score_threshold})...")
        result = do_search(args.api_url, data, args.top_k, args.score_threshold)
        hits = result.get("hits", [])
        print(f"[RAG] 命中 {len(hits)} 条相似案例:")
        for i, h in enumerate(hits):
            print(f"  [{i+1}] score={h['score']:.4f} sig={h['crash_signature']} "
                  f"signal={h['signal']} arch={h['arch']} ver={h['version']}")
            print(f"       {h['embed_text'][:100]}...")
            if h.get("analysis"):
                print(f"       (有历史分析报告)")
        # 保存到工作目录
        out_path = os.path.join(args.work_dir, "rag_hits.json")
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"[RAG] 相似案例已保存到 {out_path}")

    # 入库
    if args.action in ("ingest", "both"):
        analysis_path = os.path.join(args.work_dir, "crash_report.md")
        print(f"[RAG] 入库新案例 (status=pending)...")
        result = do_ingest(args.api_url, data, analysis_path)
        print(f"[RAG] 入库成功: id={result['id']} sig={result['crash_signature']}")
        print(f"[RAG] embed_text: {result['embed_text'][:100]}...")


if __name__ == "__main__":
    main()
