"""Cache complete feed builds and replace JSON outputs atomically."""
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
from functools import lru_cache


FEED_TABLES = {
    "MAP_LABELS", "SPLIT_LABELS", "PINNED_ANCHORS", "SKIP_ANCHORS",
    "TRIM_TERMINI", "OVERRIDE_PATHS", "SYMBOL_OWNERS", "INSET_DIVERSIONS",
}


def digest_files(paths):
    h = hashlib.sha256()
    for path in sorted(map(Path, paths)):
        h.update(str(path).encode())
        with path.open("rb") as f:
            h.update(hashlib.file_digest(f, "sha256").digest())
    return h.hexdigest()


def algorithm_digest(paths):
    h = hashlib.sha256()
    for path in paths:
        tree = ast.parse(Path(path).read_text())
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in FEED_TABLES):
                node.value = ast.Constant(None)
        h.update(ast.dump(tree, include_attributes=False).encode())
    return h.hexdigest()


@lru_cache(maxsize=1)
def library_versions():
    import numpy
    import scipy
    import PIL
    import fitz
    return {"python": platform.python_version(), "numpy": numpy.__version__,
            "scipy": scipy.__version__, "pillow": PIL.__version__,
            "pymupdf": fitz.VersionBind}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as f:
            tmp = f.name
            json.dump(value, f, separators=(",", ":"), allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


class FeedCache:
    def __init__(self, root, artwork, code, settings, tables):
        self.root = Path(root)
        self.shared = {"artwork": digest_files(artwork),
                       "algorithm": algorithm_digest(code),
                       "libraries": library_versions(), "settings": settings}
        self.tables = tables

    def key(self, feed, inputs):
        local = {name: repr(sorted((key, value) for key, value in table.items()
                                  if key[0] == feed))
                 for name, table in self.tables.items()}
        value = {**self.shared, "feed": feed, "inputs": digest_files(inputs),
                 "tables": local}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def read(self, feed, key):
        try:
            blob = json.loads((self.root / f"{feed}.json").read_text())
            if blob["key"] == key:
                data = blob["data"]
                encoded = json.dumps(data, separators=(",", ":"), allow_nan=False)
                if hashlib.sha256(encoded.encode()).hexdigest() == blob["sha256"]:
                    return data
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def write(self, feed, key, data):
        encoded = json.dumps(data, separators=(",", ":"), allow_nan=False)
        atomic_json(self.root / f"{feed}.json", {
            "key": key, "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "data": data,
        })
