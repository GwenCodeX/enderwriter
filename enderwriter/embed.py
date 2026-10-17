"""确定性向量嵌入（无第三方依赖）。

环境不保证有 sentence-transformers / ollama，因此提供**哈希特征 + L2 归一化**的
确定性嵌入：同一文本永远得到同一向量，可离线复现，单测可断言。

若上层接入了真实 embedding（Ollama / 本地模型），只需替换 `embed()` 的实现或
把外部向量直接写入 `chest.upsert(..., embedding=vec)`；检索层通过 cosine 比较，
不关心向量来源。
"""

from __future__ import annotations

import hashlib
import struct
from typing import Iterable

import numpy as np

from .lexicon import analyze

__all__ = ["DIM", "embed", "embed_many", "to_blob", "from_blob", "cosine", "cosine_matrix", "EMBED_VERSION"]

DIM = 256

#: 嵌入算法版本；换算法时递增，便于重建索引
EMBED_VERSION = 1


def _bucket(term: str) -> int:
    h = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
    return struct.unpack("<Q", h)[0] % DIM


def embed(text: str) -> np.ndarray:
    """文本 -> float32[DIM] 单位向量。空文本返回零向量。"""
    vec = np.zeros(DIM, dtype=np.float32)
    if not text:
        return vec
    terms = analyze(text)
    if not terms:
        return vec
    # 词频用 1+log(tf) 抑制高频词；同时加入整串签名增强长短语区分度
    counts: dict[str, int] = {}
    for t in terms:
        counts[t] = counts.get(t, 0) + 1
    for term, tf in counts.items():
        vec[_bucket(term)] += 1.0 + float(np.log(tf))
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec


def embed_many(texts: Iterable[str]) -> np.ndarray:
    rows = [embed(t) for t in texts]
    if not rows:
        return np.zeros((0, DIM), dtype=np.float32)
    return np.vstack(rows).astype(np.float32)


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes | None, dim: int = DIM) -> np.ndarray:
    if not blob:
        return np.zeros(dim, dtype=np.float32)
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size != dim:
        out = np.zeros(dim, dtype=np.float32)
        out[: min(dim, arr.size)] = arr[: min(dim, arr.size)]
        return out
    return arr.copy()


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def cosine_matrix(query: np.ndarray, mat: np.ndarray) -> np.ndarray:
    """批量 cosine：query[1,D] x mat[N,D] -> [N]。"""
    if mat.size == 0:
        return np.zeros((0,), dtype=np.float32)
    q = query.astype(np.float32)
    nq = float(np.linalg.norm(q))
    if nq == 0.0:
        return np.zeros((mat.shape[0],), dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    return (mat @ q) / (norms * nq)
