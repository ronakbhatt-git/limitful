"""Search by meaning: find lines that say what you mean in other words.

Each transcript line is turned into a vector by a small local model
(bge-small-en-v1.5, MIT licence, about 34 MB, run through onnxruntime, which is
already installed for Whisper). A question is turned into a vector the same
way, and the nearest lines are the answer. Nothing leaves the machine.

The vectors live in their own file, `search.db`, beside `pendant.db`. They are
derived data: the file can be deleted and rebuilt at any time, and keeping it
out of the main database keeps that one small and quick to back up.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .speakers import cache_dir

if TYPE_CHECKING:
    import numpy as np

    from .store import Store

MODEL = "bge-small-en-v1.5-int8"
_SOURCE = ("https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/"
           "ea104dacec62c0de699686887e3f920caeb4f3e3/")  # pinned revision
_FILES = {
    # local name: (path in the repository, sha256)
    "bge-small-en-v1.5-int8.onnx": (
        "onnx/model_quantized.onnx", "6c9c6101a956d62dfb5e7190c538226c0c5bb9cb27b651234b6df063ee7dbfe4"),
    "bge-small-en-v1.5-tokenizer.json": (
        "tokenizer.json", "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"),
}
# The model was trained with this in front of short questions (not in front of passages).
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
_MAX_TOKENS = 256
_BATCH = 64

# A line is indexed together with its neighbours: "Yeah." or "Under five minutes."
# means nothing alone, and on real transcripts this found more than single lines did
# (see docs/experiments.md, "Search by meaning").
NEIGHBOURS = 2            # lines taken on each side
NEIGHBOUR_GAP_MS = 60_000  # ...as long as they are within a minute, so part of the same exchange

SEARCH_LIMIT = 30
SAME_MOMENT_MS = 20_000   # hits this close together are shown as one

def ensure_model() -> dict[str, Path]:
    """Download the model files on first use and check each against its checksum."""
    paths = {}
    for name, (remote, sha256) in _FILES.items():
        path = cache_dir() / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(path.suffix + ".part")
            urllib.request.urlretrieve(_SOURCE + remote, partial)
            if hashlib.sha256(partial.read_bytes()).hexdigest() != sha256:
                partial.unlink()
                raise RuntimeError("the downloaded search model did not match its checksum")
            os.replace(partial, path)
        paths[name] = path
    return paths


class BgeEmbedder:
    """Turns text into unit vectors. Runs on the CPU, roughly 60 lines a second."""

    name = MODEL

    def __init__(self) -> None:
        import onnxruntime
        from tokenizers import Tokenizer

        paths = ensure_model()
        self._tokenizer = Tokenizer.from_file(str(paths["bge-small-en-v1.5-tokenizer.json"]))
        self._tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        self._tokenizer.enable_padding()
        self._session = onnxruntime.InferenceSession(
            str(paths["bge-small-en-v1.5-int8.onnx"]), providers=["CPUExecutionProvider"])
        self._wants_types = any(i.name == "token_type_ids" for i in self._session.get_inputs())

    def embed(self, texts: list[str], query: bool = False) -> "np.ndarray":
        import numpy as np

        if query:
            texts = [_QUERY_PREFIX + text for text in texts]
        out = []
        for i in range(0, len(texts), _BATCH):
            encoded = self._tokenizer.encode_batch(texts[i : i + _BATCH])
            feed = {"input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64)}
            if self._wants_types:
                feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
            first = self._session.run(None, feed)[0][:, 0]  # this model's sentence vector is its first token
            out.append(first / np.linalg.norm(first, axis=1, keepdims=True))
        return np.concatenate(out).astype(np.float32) if out else np.zeros((0, 384), dtype=np.float32)


_shared: list = []


def make_embedder():
    """The embedder, loaded once per process (loading it takes a moment)."""
    if not _shared:
        _shared.append(BgeEmbedder())
    return _shared[0]


def passages(rows: list[tuple[int, int, str]]) -> list[str]:
    """The text indexed for each line: the line with its close neighbours.

    `rows` are (id, start_ms, text) in time order.
    """
    out = []
    for i, (_uid, start, _text) in enumerate(rows):
        lo = i
        while lo > 0 and i - lo < NEIGHBOURS and start - rows[lo - 1][1] < NEIGHBOUR_GAP_MS:
            lo -= 1
        hi = i
        while hi + 1 < len(rows) and hi - i < NEIGHBOURS and rows[hi + 1][1] - start < NEIGHBOUR_GAP_MS:
            hi += 1
        out.append(" ".join(row[2] for row in rows[lo : hi + 1]))
    return out


class Index:
    """The vectors for a data folder's lines, and nearest-line search over them."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "search.db"
        self._loaded: tuple | None = None  # (signature, ids, matrix)

    def exists(self) -> bool:
        return self.path.exists()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.executescript(
            "CREATE TABLE IF NOT EXISTS vectors (id INTEGER PRIMARY KEY, vec BLOB NOT NULL);"
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
        return db

    def _indexed_ids(self) -> set[int]:
        if not self.exists():
            return set()
        db = self._db()
        try:
            made_with = db.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
            if made_with and made_with[0] != MODEL:
                return set()  # built by a different model: every vector must be redone
            return {row[0] for row in db.execute("SELECT id FROM vectors")}
        finally:
            db.close()

    def progress(self, store: "Store") -> dict:
        """How many lines there are and how many are in the index. Creates nothing."""
        lines = {row[0] for row in store.db.execute("SELECT id FROM utterances")}
        indexed = len(lines & self._indexed_ids())
        return {"indexed": indexed, "total": len(lines), "left": len(lines) - indexed}

    def build(
        self,
        store: "Store",
        embedder,
        on_progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> int:
        """Index every line not yet in the index. Saves after each batch, so it can
        be stopped and carried on later. Returns how many lines were added."""
        import numpy as np

        rows = store.db.execute("SELECT id, start_ms, text FROM utterances ORDER BY start_ms, id").fetchall()
        db = self._db()
        try:
            made_with = db.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
            if made_with and made_with[0] != MODEL:
                db.execute("DELETE FROM vectors")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)", (MODEL,))
            # Lines that no longer exist (a re-transcription replaced them) leave the index.
            present = {row[0] for row in rows}
            gone = [(uid,) for (uid,) in db.execute("SELECT id FROM vectors") if uid not in present]
            db.executemany("DELETE FROM vectors WHERE id = ?", gone)
            db.commit()
            have = {row[0] for row in db.execute("SELECT id FROM vectors")}
            texts = passages(rows)
            todo = [(row[0], text) for row, text in zip(rows, texts) if row[0] not in have]
            added = 0
            for i in range(0, len(todo), _BATCH):
                if should_stop():
                    break
                batch = todo[i : i + _BATCH]
                vectors = embedder.embed([text for _uid, text in batch])
                db.executemany(
                    "INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                    # Half precision: 768 bytes a line, and the ranking is unchanged.
                    [(uid, vec.astype(np.float16).tobytes()) for (uid, _t), vec in zip(batch, vectors)])
                db.commit()
                added += len(batch)
                if on_progress:
                    on_progress(added, len(todo))
            return added
        finally:
            db.close()
            self._loaded = None

    def _matrix(self):
        """(ids, vectors) for the whole index, kept in memory until the file changes."""
        import numpy as np

        stat = self.path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if self._loaded is None or self._loaded[0] != signature:
            db = self._db()
            try:
                rows = db.execute("SELECT id, vec FROM vectors ORDER BY id").fetchall()
            finally:
                db.close()
            ids = np.array([row[0] for row in rows], dtype=np.int64)
            matrix = (np.frombuffer(b"".join(row[1] for row in rows), dtype=np.float16)
                      .reshape(len(rows), -1).astype(np.float32) if rows else np.zeros((0, 384), np.float32))
            self._loaded = (signature, ids, matrix)
        return self._loaded[1], self._loaded[2]

    def search(self, embedder, query: str, limit: int = SEARCH_LIMIT,
               only: "set[int] | None" = None) -> list[tuple[int, float]]:
        """The ids of the lines closest in meaning to `query`, best first, with scores.

        `only` restricts the search to those line ids (a date range, say).
        """
        import numpy as np

        if not self.exists() or not query.strip():
            return []
        ids, matrix = self._matrix()
        if not len(ids):
            return []
        scores = matrix @ embedder.embed([query], query=True)[0]
        if only is not None:
            scores = np.where(np.isin(ids, np.fromiter(only, dtype=np.int64, count=len(only))), scores, -2.0)
        top = np.argsort(-scores)[:limit]
        return [(int(ids[i]), float(scores[i])) for i in top if scores[i] > -2.0]


def update_if_built(store: "Store", on_progress: Callable[[int, int], None] | None = None) -> int:
    """Add new lines to the index, but only if the user has built one.

    Called after transcription, so an index stays complete once it exists and
    nobody who has not asked for it downloads the model. Lines are already
    saved by then, so a failure here (no network for the first download, say)
    is swallowed: the index is simply left for the next run to finish.
    """
    index = Index(store.data_dir)
    if not index.exists():
        return 0
    try:
        return index.build(store, make_embedder(), on_progress=on_progress)
    except Exception:
        return 0


def spread(hits: list[tuple[int, float]], start_ms: dict[int, int], limit: int,
           apart_ms: int = SAME_MOMENT_MS) -> list[tuple[int, float]]:
    """Drop hits that sit beside a better one.

    Neighbouring lines are indexed with much the same text, so they come back
    in clumps; one line per moment says as much and leaves room for others.
    """
    kept: list[tuple[int, float]] = []
    for uid, score in hits:
        at = start_ms.get(uid)
        if at is None or any(abs(at - start_ms[other]) < apart_ms for other, _s in kept):
            continue
        kept.append((uid, score))
        if len(kept) == limit:
            break
    return kept


_indexes: dict[Path, Index] = {}


def index_for(data_dir: Path) -> Index:
    """One Index per data folder for the life of the process, so its vectors stay loaded."""
    path = Path(data_dir).resolve()
    if path not in _indexes:
        _indexes[path] = Index(path)
    return _indexes[path]
