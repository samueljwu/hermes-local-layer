from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - fallback for minimal environments
    yaml = None


@dataclass
class ScoutConfig:
    # Dummy defaults: intentionally conservative and easy to edit later.
    languages: list[str] = field(default_factory=lambda: ["Python", "TypeScript", "Rust"])
    topics: list[str] = field(default_factory=lambda: ["llm", "agents", "developer-tools", "mlops"])
    keywords: list[str] = field(default_factory=lambda: ["agent", "llm", "developer tools", "automation"])
    min_stars: int = 50
    max_stars: int = 20000
    pushed_within_days: int = 45
    min_commits_per_month: int = 5
    commit_months: int = 6
    include_current_month: bool = False
    max_candidates: int = 200
    search_pages_per_query: int = 1
    max_api_repos_for_commit_check: int = 80
    shortlist_size: int = 25
    allowed_licenses: list[str] = field(default_factory=lambda: ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0"])
    contribution_labels: list[str] = field(default_factory=lambda: ["good first issue", "help wanted", "documentation", "bug"])
    interest_roots: list[str] = field(default_factory=list)
    cache_ttl_hours: int = 24

    @classmethod
    def default(cls) -> "ScoutConfig":
        return cls()


def _coerce_config(data: dict[str, Any]) -> ScoutConfig:
    cfg = ScoutConfig.default()
    for key, value in data.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Tiny fallback parser for the simple config shape used here."""
    out: dict[str, Any] = {}
    current_key: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("  - ") and current_key:
            out.setdefault(current_key, []).append(line[4:].strip().strip('"\''))
            continue
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            current_key = key
            if value == "":
                out[key] = []
            elif value.isdigit():
                out[key] = int(value)
            else:
                out[key] = value.strip('"\'')
    return out


def load_config(path: str | Path) -> ScoutConfig:
    path = Path(path)
    if not path.exists():
        return ScoutConfig.default()
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        data = yaml.safe_load(text) or {}
    else:
        data = _parse_simple_yaml(text)
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a mapping: {path}")
    return _coerce_config(data)
