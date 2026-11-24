"""埋め込みの計算。

  fastembed : sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2（384次元、Apache-2.0、日本語と英語を含む
              50 以上の言語）。fastembed（ONNX、CPU）が入っていれば使う。当初の候補 multilingual-e5-small は
              fastembed の対応モデルに無かったため、同じ 384 次元のこのモデルにした（ADR-0004）。
  hash : 文字 n-gram のハッシュによる 384 次元ベクトル。追加の導入なしで動くが、捉えるのは表記の近さだけで
         意味の近さではない。モデルを置く余裕がない環境向けの代用で、記録上も別のモデル名（hash-ngram-384）になる。
どちらで作ったベクトルかは embeddings.model に残り、検索は同じモデルのベクトルどうしでだけ行う。
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from functools import lru_cache
from typing import Protocol

import numpy as np

DIM = 384


class Embedder(Protocol):
    name: str
    semantic: bool

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]: ...


class HashEmbedder:
    name = "hash-ngram-384"
    semantic = False

    @staticmethod
    def _tokens(text: str) -> list[str]:
        t = unicodedata.normalize("NFKC", text).lower()
        words = re.findall(r"[a-z0-9]+", t)
        cjk = "".join(re.findall(r"[぀-ヿ一-鿿]+", t))
        grams = [cjk[i:i + 2] for i in range(len(cjk) - 1)]
        tri = [w[i:i + 3] for w in words for i in range(max(len(w) - 2, 1))]
        return words + tri + grams

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        out = []
        for text in texts:
            v = np.zeros(DIM, dtype=np.float32)
            for tok in self._tokens(text):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
                v[h % DIM] += 1.0 if (h >> 63) & 1 else -1.0
            n = float(np.linalg.norm(v))
            out.append((v / n if n > 0 else v).tolist())
        return out


class FastEmbedEmbedder:
    semantic = True

    def __init__(self, model: str) -> None:
        from fastembed import TextEmbedding  # 任意の依存（pip install '.[embed]'）

        self.name = model
        from aar.config import get_settings

        cache = get_settings().data_dir / "models"   # 一時フォルダだと再起動で消えるので、データ用の場所に置く
        cache.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=model, cache_dir=str(cache))

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        # e5 系だけは用途を示す接頭辞が要る
        prefix = ("query: " if is_query else "passage: ") if "e5" in self.name.lower() else ""
        vecs = self._model.embed([prefix + t[:2000] for t in texts])
        out = []
        for v in vecs:
            arr = np.asarray(v, dtype=np.float32)
            n = float(np.linalg.norm(arr))
            out.append((arr / n if n > 0 else arr).tolist())
        return out


@lru_cache(maxsize=4)
def get_embedder(model: str) -> Embedder:
    """設定されたモデルを返す。導入されていなければ hash にフォールバックする（名前で区別できる）。"""
    if model in ("hash", HashEmbedder.name):
        return HashEmbedder()
    try:
        emb = FastEmbedEmbedder(model)
        assert len(emb.embed(["確認"])[0]) == DIM
        return emb
    except Exception:  # noqa: BLE001 - 導入されていない・モデルを取得できない場合はすべて代用に回す
        return HashEmbedder()


def cosine(a: list[float], b: list[float]) -> float:
    s = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0
