#!/usr/bin/env python3
"""server.py - RAG Crash 知识库服务

用法:
  python3 server.py                      # 启动 API 服务 (端口 8000)
  python3 server.py --batch-ingest DIR   # 批量入库 DIR 下的 report_*/structured.json

接口:
  POST /embed   - 文本 → 向量（不入库）
  POST /ingest  - structured.json → 特征提取 → embedding → Qdrant
  POST /search  - structured.json → 检索相似案例
  GET  /health  - 健康检查
"""

import hashlib
import os
import sys
import time
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

# ---------- 特征提取（从 structured.json 提取 embedding 输入和 metadata） ----------

def extract_features(data: dict) -> dict:
    """从 structured.json 提取 embedding 输入和 metadata

    embedding 输入: 版本 + 架构 + 信号 + 栈顶5帧 module!func + assert位置 + 代码出错位置
    """
    crash_info = data.get("crash_info", {})
    device_info = data.get("device_info", {})
    arch = data.get("arch", "unknown")
    signal = crash_info.get("signal", "unknown")
    version = device_info.get("ver", "unknown")

    bt = data.get("current_backtrace", [])
    crashed = next((t for t in bt if t.get("crashed")), bt[0] if bt else None)
    frames = crashed.get("frames", []) if crashed else []

    top5_parts = []
    assert_location = None
    code_location = None
    for i, fr in enumerate(frames[:5]):
        mod = fr.get("module", "?")
        func = fr.get("func") or "?"
        top5_parts.append(f"{mod}!{func}")
        if i == 0 and fr.get("file") and fr.get("line"):
            assert_location = f"{fr['file']}:{fr['line']}"
        if fr.get("file") and fr.get("line") and not code_location:
            code_location = f"{fr['file']}:{fr['line']}"

    top5_text = " -> ".join(top5_parts) if top5_parts else "(no frames)"

    embed_parts = [
        f"version: {version}",
        f"arch: {arch}",
        f"signal: {signal}",
        f"stack: {top5_text}",
    ]
    if assert_location:
        embed_parts.append(f"assert at: {assert_location}")
    if code_location and code_location != assert_location:
        embed_parts.append(f"code at: {code_location}")

    embed_text = ", ".join(embed_parts)
    sig_str = " | ".join(top5_parts)
    sig_hash = hashlib.md5(sig_str.encode()).hexdigest()[:12]

    return {
        "embed_text": embed_text,
        "metadata": {
            "crash_signature": sig_hash,
            "signature_raw": sig_str,
            "signal": signal,
            "arch": arch,
            "version": version,
            "device": device_info.get("plat", "unknown"),
            "assert_location": assert_location,
            "code_location": code_location,
            "frame_count": len(frames),
            "source_url": data.get("source_url", ""),
            "source": data.get("source", ""),
        },
    }


# ---------- 延迟加载重资源 ----------

_model = None
_client = None

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLLECTION = os.environ.get("COLLECTION", "crash_kb")
MODEL_NAME = os.environ.get("MODEL_NAME", "BAAI/bge-m3")


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        print(f"加载模型 {MODEL_NAME} ...")
        _model = SentenceTransformer(MODEL_NAME)
        print("模型加载完成")
    return _model


def get_client():
    global _client
    if _client is None:
        from qdrant_client import QdrantClient
        _client = QdrantClient(url=QDRANT_URL, check_compatibility=False)
    return _client


def ensure_collection():
    from qdrant_client.models import Distance, VectorParams
    client = get_client()
    collections = client.get_collections().collections
    if not any(c.name == COLLECTION for c in collections):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
        )
        print(f"创建 collection: {COLLECTION}")


# ---------- FastAPI 应用 ----------

app = FastAPI(title="RAG Crash KB", version="0.1.0")


class EmbedRequest(BaseModel):
    text: str

class EmbedResponse(BaseModel):
    vector: list[float]
    dim: int

class IngestRequest(BaseModel):
    structured_json: dict
    status: str = "pending"
    analysis: Optional[str] = None
    solution: Optional[str] = None

class IngestResponse(BaseModel):
    id: int
    crash_signature: str
    embed_text: str

class SearchRequest(BaseModel):
    structured_json: dict
    top_k: int = 5
    score_threshold: float = 0.0
    status: Optional[str] = None

class SearchHit(BaseModel):
    score: float
    crash_signature: str
    source_url: str
    signal: str
    arch: str
    version: str
    status: str
    analysis: Optional[str] = None
    solution: Optional[str] = None
    embed_text: str

class SearchResponse(BaseModel):
    hits: list[SearchHit]
    total: int


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    model = get_model()
    vec = model.encode([req.text])[0]
    return EmbedResponse(vector=vec.tolist(), dim=len(vec))


@app.post("/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest):
    ensure_collection()
    feat = extract_features(req.structured_json)
    model = get_model()
    vec = model.encode([feat["embed_text"]])[0]

    from qdrant_client.models import PointStruct
    client = get_client()
    point_id = int(hashlib.md5(feat["metadata"]["source_url"].encode()).hexdigest()[:15], 16)

    payload = {
        **feat["metadata"],
        "embed_text": feat["embed_text"],
        "status": req.status,
        "analysis": req.analysis,
        "solution": req.solution,
    }

    client.upsert(
        collection_name=COLLECTION,
        points=[PointStruct(id=point_id, vector=vec.tolist(), payload=payload)],
    )
    return IngestResponse(
        id=point_id,
        crash_signature=feat["metadata"]["crash_signature"],
        embed_text=feat["embed_text"],
    )


@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest):
    feat = extract_features(req.structured_json)
    model = get_model()
    query_vec = model.encode([feat["embed_text"]])[0]

    from qdrant_client.models import Filter, FieldCondition, MatchValue
    client = get_client()

    query_filter = None
    if req.status is not None:
        query_filter = Filter(
            must=[FieldCondition(key="status", match=MatchValue(value=req.status))]
        )

    results = client.query_points(
        collection_name=COLLECTION,
        query=query_vec.tolist(),
        limit=req.top_k,
        query_filter=query_filter,
    ).points

    my_url = feat["metadata"]["source_url"]
    hits = []
    for r in results:
        if r.payload.get("source_url") == my_url:
            continue
        if r.score < req.score_threshold:
            continue
        hits.append(SearchHit(
            score=r.score,
            crash_signature=r.payload.get("crash_signature", ""),
            source_url=r.payload.get("source_url", ""),
            signal=r.payload.get("signal", ""),
            arch=r.payload.get("arch", ""),
            version=r.payload.get("version", ""),
            status=r.payload.get("status", ""),
            analysis=r.payload.get("analysis"),
            solution=r.payload.get("solution"),
            embed_text=r.payload.get("embed_text", ""),
        ))

    return SearchResponse(hits=hits, total=len(hits))


# ---------- 批量入库（命令行模式） ----------

def batch_ingest(scan_dir: str):
    """扫描 scan_dir 下的 report_*/structured.json，通过 /ingest API 批量入库"""
    import glob
    import json
    import urllib.request

    paths = sorted(glob.glob(os.path.join(scan_dir, "report_*/structured.json")))
    print(f"扫描到 {len(paths)} 个 structured.json")

    api_url = f"http://localhost:8000"
    ok, fail = 0, 0
    t0 = time.time()
    for i, p in enumerate(paths):
        with open(p) as f:
            data = json.load(f)
        payload = json.dumps({"structured_json": data, "status": "pending"}).encode()
        req = urllib.request.Request(
            f"{api_url}/ingest", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp_data = json.loads(resp.read())
            ok += 1
            if i < 3 or i % 10 == 0:
                print(f"[{i+1}/{len(paths)}] {resp_data['crash_signature']} <- {resp_data['embed_text'][:70]}...")
        except Exception as e:
            fail += 1
            print(f"[{i+1}/{len(paths)}] FAIL {p}: {e}")

    print(f"\n入库完成: ok={ok} fail={fail} 耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--batch-ingest":
        scan_dir = sys.argv[2] if len(sys.argv) > 2 else "/home/xujialiu/rag-server/samples"
        batch_ingest(scan_dir)
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000)
