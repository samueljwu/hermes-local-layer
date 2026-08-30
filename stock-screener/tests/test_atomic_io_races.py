import tempfile
import unittest
from pathlib import Path

from stock_screener.atomic_io import atomic_write, stage_and_promote_bundle


class DescriptorBoundPublicationRaceTests(unittest.TestCase):
    def _swap_parent(self, parent: Path, moved: Path, outside: Path) -> None:
        parent.rename(moved)
        parent.symlink_to(outside, target_is_directory=True)

    def _assert_no_publication_temps(self, *directories: Path) -> None:
        for directory in directories:
            leaked = [path.name for path in directory.iterdir() if path.name.startswith(".")]
            self.assertEqual(leaked, [], f"temporary publication files leaked in {directory}: {leaked}")

    def test_atomic_write_remains_bound_when_parent_is_swapped_during_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parent = root / "owned"
            moved = root / "owned-original"
            outside = root / "outside"
            parent.mkdir()
            outside.mkdir()
            (parent / "target.txt").write_text("owned-old", encoding="utf-8")
            (outside / "target.txt").write_text("external-old", encoding="utf-8")

            swapped = False

            def payload_writer(stage: Path) -> None:
                nonlocal swapped
                if not swapped:
                    self._swap_parent(parent, moved, outside)
                    swapped = True
                stage.write_text("owned-new", encoding="utf-8")

            atomic_write(parent / "target.txt", payload_writer)

            self.assertEqual((outside / "target.txt").read_text(encoding="utf-8"), "external-old")
            self.assertEqual((moved / "target.txt").read_text(encoding="utf-8"), "owned-new")
            self._assert_no_publication_temps(moved, outside)

    def test_bundle_remains_bound_when_shared_parent_is_swapped_during_staging(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parent = root / "owned"
            moved = root / "owned-original"
            outside = root / "outside"
            parent.mkdir()
            outside.mkdir()
            for name in ("one.txt", "two.txt"):
                (parent / name).write_text(f"owned-old-{name}", encoding="utf-8")
                (outside / name).write_text(f"external-old-{name}", encoding="utf-8")

            swapped = False

            def write_first(stage: Path) -> None:
                nonlocal swapped
                if not swapped:
                    self._swap_parent(parent, moved, outside)
                    swapped = True
                stage.write_text("owned-new-one", encoding="utf-8")

            stage_and_promote_bundle([
                (parent / "one.txt", write_first),
                (parent / "two.txt", lambda stage: stage.write_text("owned-new-two", encoding="utf-8")),
            ])

            self.assertEqual((outside / "one.txt").read_text(encoding="utf-8"), "external-old-one.txt")
            self.assertEqual((outside / "two.txt").read_text(encoding="utf-8"), "external-old-two.txt")
            self.assertEqual((moved / "one.txt").read_text(encoding="utf-8"), "owned-new-one")
            self.assertEqual((moved / "two.txt").read_text(encoding="utf-8"), "owned-new-two")
            self._assert_no_publication_temps(moved, outside)


if __name__ == "__main__":
    unittest.main()
