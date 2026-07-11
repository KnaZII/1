"""Server registry — manages the ~/.meridian/servers index file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SERVER_ROLE_EXIT = "exit"
SERVER_ROLE_RELAY = "relay"
_SERVER_ROLES = {SERVER_ROLE_EXIT, SERVER_ROLE_RELAY}


@dataclass
class ServerEntry:
    """A known server: host, SSH user, display name, and SSH port."""

    host: str
    user: str = "root"
    name: str = ""
    role: str = SERVER_ROLE_EXIT
    port: int = 22

    def __str__(self) -> str:
        parts = [self.host, self.user]
        if self.role == SERVER_ROLE_EXIT:
            if self.name:
                parts.append(self.name)
            # Append port only when non-default (backwards-compatible)
            if self.port != 22:
                if not self.name:
                    parts.append("-")
                parts.append(f"port={self.port}")
            return " ".join(parts)

        # Relay entries need an explicit role marker so fresh machines can
        # distinguish them from exit servers without relying on local cache.
        parts.extend([self.name or "-", self.role])
        if self.port != 22:
            parts.append(f"port={self.port}")
        return " ".join(parts)

    @classmethod
    def from_line(cls, line: str) -> ServerEntry | None:
        """Parse a line from the servers file. Returns None for comments/blanks."""
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return None
        parts = stripped.split()
        if len(parts) < 2:
            return None

        # Extract port=N from the end if present
        port = 22
        if parts[-1].startswith("port="):
            try:
                port = int(parts[-1].split("=", 1)[1])
            except (ValueError, IndexError):
                pass
            parts = parts[:-1]

        if len(parts) >= 4 and parts[3] in _SERVER_ROLES:
            name = "" if parts[2] == "-" else parts[2]
            return cls(host=parts[0], user=parts[1], name=name, role=parts[3], port=port)
        name = parts[2] if len(parts) > 2 else ""
        # Handle the "-" placeholder for name when port was present
        if name == "-":
            name = ""
        return cls(host=parts[0], user=parts[1], name=name, port=port)


class ServerRegistry:
    """CRUD operations on the servers index file.

    File format: one server per line, space-separated: "host user [name]"
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text().splitlines()

    def _write_lines(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(lines) + "\n" if lines else "")

    def list(self) -> list[ServerEntry]:
        """Return all registered servers."""
        entries = []
        for raw in self._read_lines():
            entry = ServerEntry.from_line(raw)
            if entry:
                entries.append(entry)
        return entries

    def count(self) -> int:
        return len(self.list())

    def find(self, query: str) -> ServerEntry | None:
        """Find a server by IP or name (first match)."""
        for entry in self.list():
            if entry.host == query or entry.name == query:
                return entry
        return None

    def add(self, entry: ServerEntry) -> None:
        """Add a server, deduplicating by host IP."""
        lines = self._read_lines()
        # Remove existing entry for same host
        new_lines = []
        for raw in lines:
            existing = ServerEntry.from_line(raw)
            if existing and existing.host == entry.host:
                continue
            new_lines.append(raw)
        new_lines.append(str(entry))
        self._write_lines(new_lines)

    def remove(self, query: str) -> bool:
        """Remove a server by IP or name. Returns True if found and removed."""
        lines = self._read_lines()
        new_lines = []
        removed = False
        for raw in lines:
            existing = ServerEntry.from_line(raw)
            if existing and (existing.host == query or existing.name == query):
                removed = True
                continue
            new_lines.append(raw)
        if removed:
            self._write_lines(new_lines)
        return removed
