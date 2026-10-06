import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stock_screener.atomic_io import atomic_write, stage_and_promote_bundle, stage_and_promote_generation


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

    def test_generation_directory_and_files_remain_bound_during_parent_swap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parent = root / "owned"
            moved = root / "owned-original"
            outside = root / "outside"
            parent.mkdir()
            outside.mkdir()
            for base in (parent, outside):
                (base / "charts").mkdir()
                (base / "charts" / "old.svg").write_text(base.name, encoding="utf-8")
                (base / "index.html").write_text(f"{base.name}-old-html", encoding="utf-8")
                (base / "summary.json").write_text(f"{base.name}-old-summary", encoding="utf-8")

            def populate(stage: Path) -> None:
                self._swap_parent(parent, moved, outside)
                (stage / "new.svg").write_text("owned-new-chart", encoding="utf-8")

            stage_and_promote_generation(
                [(parent / "charts", populate)],
                [
                    (parent / "index.html", lambda stage: stage.write_text("owned-new-html", encoding="utf-8")),
                    (parent / "summary.json", lambda stage: stage.write_text("owned-new-summary", encoding="utf-8")),
                ],
            )

            self.assertEqual((outside / "charts" / "old.svg").read_text(encoding="utf-8"), "outside")
            self.assertEqual((outside / "index.html").read_text(encoding="utf-8"), "outside-old-html")
            self.assertEqual((outside / "summary.json").read_text(encoding="utf-8"), "outside-old-summary")
            self.assertEqual((moved / "charts" / "new.svg").read_text(encoding="utf-8"), "owned-new-chart")
            self.assertEqual((moved / "index.html").read_text(encoding="utf-8"), "owned-new-html")
            self.assertEqual((moved / "summary.json").read_text(encoding="utf-8"), "owned-new-summary")
            self._assert_no_publication_temps(moved, outside)

    def test_generation_rolls_back_directory_and_files_on_mid_promotion_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            charts = root / "charts"
            charts.mkdir()
            (charts / "old.svg").write_text("old-chart", encoding="utf-8")
            html = root / "index.html"
            summary = root / "summary.json"
            html.write_text("old-html", encoding="utf-8")
            summary.write_text("old-summary", encoding="utf-8")
            real_replace = __import__("os").replace
            calls = 0

            def fail_mid_promotion(src, dst, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise OSError("injected generation promotion failure")
                return real_replace(src, dst, **kwargs)

            with mock.patch("stock_screener.atomic_io.os.replace", side_effect=fail_mid_promotion):
                with self.assertRaisesRegex(OSError, "injected generation promotion failure"):
                    stage_and_promote_generation(
                        [(charts, lambda stage: (stage / "new.svg").write_text("new-chart", encoding="utf-8"))],
                        [
                            (html, lambda stage: stage.write_text("new-html", encoding="utf-8")),
                            (summary, lambda stage: stage.write_text("new-summary", encoding="utf-8")),
                        ],
                    )

            self.assertEqual((charts / "old.svg").read_text(encoding="utf-8"), "old-chart")
            self.assertEqual(html.read_text(encoding="utf-8"), "old-html")
            self.assertEqual(summary.read_text(encoding="utf-8"), "old-summary")
            self._assert_no_publication_temps(root)


if __name__ == "__main__":
    unittest.main()
