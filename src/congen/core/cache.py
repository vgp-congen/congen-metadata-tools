"""A small disk cache for remote lookups.

Assembly reports and S3 listings are fetched repeatedly across a
full-corpus run and change rarely, so they are cached on disk. Parsed
headers are keyed by the object's **ETag**, which means a re-upload
invalidates its entry naturally rather than needing a TTL guess.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ENV_CACHE_DIR = "CONGEN_CACHE_DIR"
DEFAULT_SUBDIR = "congen"


def default_cache_dir() -> Path:
    override = os.environ.get(ENV_CACHE_DIR)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / DEFAULT_SUBDIR


@dataclass
class Cache:
    """A namespaced JSON disk cache.

    ``enabled=False`` makes every lookup a miss and every store a no-op,
    which is what ``--no-cache`` sets rather than having callers branch.
    """

    directory: Path | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        self.directory = Path(self.directory) if self.directory else default_cache_dir()

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        assert self.directory is not None
        # Shard by the first two hex characters so no directory grows
        # unmanageably large.
        return self.directory / namespace / digest[:2] / f"{digest}.json"

    def get(self, namespace: str, key: str, *, ttl: float | None = None) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(namespace, key)
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        except OSError:
            return None
        if ttl is not None:
            stored_at = payload.get("stored_at")
            if not isinstance(stored_at, (int, float)) or time.time() - stored_at > ttl:
                return None
        return payload.get("value")

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"stored_at": time.time(), "key": key, "value": value}
        # Write-then-rename so a concurrent reader never sees a partial
        # file. The temporary name must be unique per writer: validate
        # runs species in parallel and they hit the same keys (the
        # accession listing, most obviously), so a shared ".tmp" name
        # meant one writer renamed the other's file out from under it.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def memoize(
        self,
        namespace: str,
        key: str,
        producer: Callable[[], Any],
        *,
        ttl: float | None = None,
    ) -> Any:
        """Return the cached value, or produce, store and return it."""
        hit = self.get(namespace, key, ttl=ttl)
        if hit is not None:
            return hit
        value = producer()
        self.set(namespace, key, value)
        return value

    def clear(self, namespace: str | None = None) -> int:
        """Delete cached entries. Returns how many files were removed."""
        assert self.directory is not None
        root = self.directory / namespace if namespace else self.directory
        if not root.exists():
            return 0
        removed = 0
        for path in root.rglob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
