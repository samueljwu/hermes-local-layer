from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("feed_ops_lock_test", ROOT / "_tools" / "feed_ops.py"),
        ("feed_renderer_lock_test", ROOT / "_tools" / "render_feed_page.py"),
        (
            "feed_pin_lock_test",
            Path("/home/hermes/.hermes/scripts/update_feed_sources_message.py"),
        ),
    ],
)
def test_feed_locks_reject_symlinks_without_truncating_target(tmp_path, monkeypatch, name, path):
    module = load_module(name, path)
    feed_root = tmp_path / "feed"
    feed_root.mkdir()
    target = tmp_path / "protected.txt"
    target.write_text("preserve me\n", encoding="utf-8")
    lock = feed_root / ".feed_ops.lock"
    lock.symlink_to(target)

    if hasattr(module, "BASE"):
        monkeypatch.setattr(module, "BASE", feed_root)
    else:
        monkeypatch.setattr(module, "FEED_ROOT", feed_root)
    monkeypatch.setattr(module, "LOCK_PATH", lock)

    with pytest.raises(OSError):
        with module.feed_lock():
            pass

    assert target.read_text(encoding="utf-8") == "preserve me\n"
