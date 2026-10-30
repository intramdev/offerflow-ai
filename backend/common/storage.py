"""Organization-scoped file storage.

The local adapter serves development only; staging uses an S3-compatible
adapter behind the same key discipline (Package F). Keys always carry the
owning organization first. Traversal outside the file root is refused, and
callers must still check membership before reading or writing.
"""

from __future__ import annotations

from pathlib import Path


class UnsafeStorageKey(ValueError):
    """Raised when a storage key or segment fails validation."""


def _check_segment(segment: str) -> None:
    if (
        not isinstance(segment, str)
        or not segment
        or segment in (".", "..")
        or "/" in segment
        or "\\" in segment
        or "\x00" in segment
    ):
        raise UnsafeStorageKey(f"Rejected storage path segment: {segment!r}")


class LocalFileAdapter:
    """Filesystem adapter rooted at a project-scoped directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def build_key(self, organization_id: str, *parts: str) -> str:
        """Build an organization-scoped key, rejecting unsafe segments."""
        for segment in (organization_id, *parts):
            _check_segment(segment)
        return "/".join((organization_id, *parts))

    def _resolve(self, key: str) -> Path:
        if not isinstance(key, str) or not key or key.startswith("/"):
            raise UnsafeStorageKey(f"Rejected storage key: {key!r}")
        if ".." in key.split("/"):
            raise UnsafeStorageKey(f"Rejected storage key: {key!r}")
        path = (self._root / key).resolve()
        try:
            path.relative_to(self._root.resolve())
        except ValueError:
            raise UnsafeStorageKey(f"Key escapes the file root: {key!r}") from None
        return path

    def save_bytes(self, key: str, data: bytes) -> str:
        """Write bytes to a validated key, creating parent directories."""
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def read_bytes(self, key: str) -> bytes:
        """Read bytes from a validated key. Missing keys raise FileNotFoundError."""
        return self._resolve(key).read_bytes()

    def exists(self, key: str) -> bool:
        """Return whether a validated key exists."""
        return self._resolve(key).exists()

    def delete(self, key: str) -> bool:
        """Delete a validated key. Missing keys are a silent success."""
        path = self._resolve(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True
