from __future__ import annotations

from pathlib import Path
import json
import os

from .types import AuthRecord


class AuthStore:
    def __init__(self, auth_file_path: Path) -> None:
        self.auth_file_path = auth_file_path

    def load(self) -> AuthRecord | None:
        if not self.auth_file_path.exists():
            return None
        data = json.loads(self.auth_file_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid auth file format")
        return AuthRecord.from_dict(data)

    def save(self, record: AuthRecord) -> None:
        self._ensure_parent_dir()
        temp_path = self.auth_file_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, self.auth_file_path)
        os.chmod(self.auth_file_path, 0o600)

    def delete(self) -> None:
        if self.auth_file_path.exists():
            self.auth_file_path.unlink()

    def _ensure_parent_dir(self) -> None:
        self.auth_file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.auth_file_path.parent, 0o700)
        except OSError:
            pass
