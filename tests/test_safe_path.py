"""
Containment tests for core/safe_path.py.

Everything here runs inside a temporary directory created per test. Nothing
touches the real home directory, and nothing needs the network.
"""

from __future__ import annotations

import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.safe_path import (                                    # noqa: E402
    ArchiveTooLarge,
    PathEscape,
    UnsafeArchiveMember,
    classify_path,
    is_within,
    real,
    resolve_within,
    resolve_within_any,
    safe_extract,
)


class _TempRootCase(unittest.TestCase):
    """Gives each test a `self.root` inside a private temp dir, plus an
    `self.outside` sibling that nothing is allowed to reach."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = real(self._tmp.name)
        self.root = base / "root"
        self.outside = base / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("private", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()


class TestContainment(_TempRootCase):

    def test_relative_path_joins_onto_root(self):
        got = resolve_within(self.root, "notes/todo.txt")
        self.assertEqual(got, self.root / "notes" / "todo.txt")

    def test_path_need_not_exist(self):
        got = resolve_within(self.root, "does/not/exist/yet.txt")
        self.assertTrue(is_within(self.root, got))

    def test_root_itself_is_within_root(self):
        self.assertTrue(is_within(self.root, self.root))
        self.assertEqual(resolve_within(self.root, "."), self.root)

    def test_dotdot_traversal_is_rejected(self):
        for attempt in (
            "../outside/secret.txt",
            "a/../../outside/secret.txt",
            "../../../../../../etc/passwd",
            "sub/../../outside",
        ):
            with self.subTest(attempt=attempt):
                with self.assertRaises(PathEscape):
                    resolve_within(self.root, attempt)

    def test_dotdot_that_stays_inside_is_allowed(self):
        # `a/../b` is just `b`. Traversal is only a problem when it leaves.
        got = resolve_within(self.root, "a/../b.txt")
        self.assertEqual(got, self.root / "b.txt")

    def test_absolute_path_outside_root_is_rejected(self):
        with self.assertRaises(PathEscape):
            resolve_within(self.root, str(self.outside / "secret.txt"))

    def test_absolute_path_inside_root_is_accepted(self):
        target = self.root / "inside.txt"
        self.assertEqual(resolve_within(self.root, str(target)), target)

    def test_absolute_path_rejected_outright_when_disallowed(self):
        # Even an absolute path that *is* inside the root: where only a sub-path
        # is meaningful, a leading slash is a bug, not a location.
        target = self.root / "inside.txt"
        with self.assertRaises(PathEscape):
            resolve_within(self.root, str(target), allow_absolute=False)

    def test_windows_style_absolute_is_rejected(self):
        # PurePosixPath does not consider these absolute, so without the explicit
        # drive check they would be joined onto the root as a relative name.
        for attempt in (r"C:\Windows\System32\drivers\etc\hosts", r"\\server\share\x"):
            with self.subTest(attempt=attempt):
                with self.assertRaises(PathEscape):
                    resolve_within(self.root, attempt, allow_absolute=False)

    def test_sibling_with_shared_prefix_is_not_inside(self):
        # The classic startswith() bug: /tmp/x/root vs /tmp/x/root_evil.
        sibling = self.root.parent / (self.root.name + "_evil")
        sibling.mkdir()
        self.assertFalse(is_within(self.root, sibling))
        with self.assertRaises(PathEscape):
            resolve_within(self.root, str(sibling / "f.txt"))

    def test_empty_candidate_raises_value_error(self):
        for attempt in ("", "   ", None):
            with self.subTest(attempt=attempt):
                with self.assertRaises(ValueError):
                    resolve_within(self.root, attempt)

    def test_is_within_never_raises_on_nonsense(self):
        self.assertFalse(is_within(self.root, "\x00bad"))


@unittest.skipUnless(hasattr(os, "symlink"), "platform has no symlinks")
class TestSymlinks(_TempRootCase):

    def _symlink(self, link: Path, target: Path) -> bool:
        try:
            link.symlink_to(target)
            return True
        except (OSError, NotImplementedError):
            # Windows without developer mode. Skip rather than fail.
            return False

    def test_symlink_pointing_outside_root_is_rejected(self):
        link = self.root / "escape"
        if not self._symlink(link, self.outside):
            self.skipTest("cannot create symlinks here")
        with self.assertRaises(PathEscape):
            resolve_within(self.root, "escape/secret.txt")
        self.assertFalse(is_within(self.root, link))

    def test_symlink_staying_inside_root_is_fine(self):
        (self.root / "real").mkdir()
        link = self.root / "alias"
        if not self._symlink(link, self.root / "real"):
            self.skipTest("cannot create symlinks here")
        got = resolve_within(self.root, "alias/file.txt")
        self.assertEqual(got, self.root / "real" / "file.txt")

    def test_symlinked_root_still_contains_its_children(self):
        # macOS puts the temp dir behind a symlink, so a root reached through one
        # must still contain its own files or every uploaded file fails.
        alias = self.root.parent / "root_alias"
        if not self._symlink(alias, self.root):
            self.skipTest("cannot create symlinks here")
        self.assertTrue(is_within(alias, self.root / "child.txt"))
        self.assertTrue(is_within(self.root, alias / "child.txt"))


class TestMultipleRoots(_TempRootCase):

    def test_first_matching_root_wins(self):
        other = self.outside            # a second *permitted* root here
        target = other / "secret.txt"
        got = resolve_within_any([self.root, other], str(target))
        self.assertEqual(got, target)

    def test_rejected_when_inside_none_of_them(self):
        third = self.root.parent / "third"
        third.mkdir()
        with self.assertRaises(PathEscape):
            resolve_within_any([self.root, self.outside], str(third / "f.txt"))

    def test_relative_resolves_against_the_first_root(self):
        got = resolve_within_any([self.root, self.outside], "a.txt")
        self.assertEqual(got, self.root / "a.txt")

    def test_no_roots_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            resolve_within_any([], "a.txt")


class TestClassifyPath(_TempRootCase):

    def test_categories(self):
        self.assertEqual(classify_path(Path.home() / "Desktop" / "x.txt"), "home")
        self.assertEqual(classify_path(self.root / "x.txt"), "temp")
        self.assertIn(classify_path("/etc/hosts"), {"system", "home", "temp"})

    def test_never_raises(self):
        self.assertIsInstance(classify_path("\x00"), str)


# ── Archives ─────────────────────────────────────────────────────────────────

class TestZipExtraction(_TempRootCase):

    def _zip(self, entries: "list[tuple[str, bytes]]", **kw) -> Path:
        path = self.root / "in.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)
            for name, attr in kw.get("raw_attrs", []):
                info = zipfile.ZipInfo(name)
                info.external_attr = attr
                zf.writestr(info, kw.get("link_target", b"/etc/passwd"))
        return path

    def test_ordinary_zip_extracts(self):
        src = self._zip([("a.txt", b"hello"), ("sub/b.txt", b"world")])
        dest = self.root / "out"
        result = safe_extract(src, dest)
        self.assertEqual(result.files, 2)
        self.assertEqual((dest / "a.txt").read_text(), "hello")
        self.assertEqual((dest / "sub" / "b.txt").read_text(), "world")
        self.assertEqual(result.total_bytes, 10)

    def test_zip_slip_dotdot_is_refused(self):
        src = self._zip([("../escaped.txt", b"pwned")])
        with self.assertRaises(UnsafeArchiveMember):
            safe_extract(src, self.root / "out")
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_zip_absolute_member_is_refused(self):
        src = self._zip([("/etc/cron.d/evil", b"pwned")])
        with self.assertRaises(UnsafeArchiveMember):
            safe_extract(src, self.root / "out")

    def test_zip_backslash_traversal_is_refused(self):
        src = self._zip([(r"..\..\escaped.txt", b"pwned")])
        with self.assertRaises(UnsafeArchiveMember):
            safe_extract(src, self.root / "out")

    def test_zip_symlink_member_is_refused(self):
        # 0o120777 << 16 — a unix symlink recorded in a zip. Python's own
        # extractall writes these out as real symlinks.
        src = self._zip([], raw_attrs=[("link", (0o120777 << 16))])
        with self.assertRaises(UnsafeArchiveMember) as ctx:
            safe_extract(src, self.root / "out")
        self.assertIn("symbolic link", str(ctx.exception))

    def test_zip_windows_device_name_is_refused(self):
        src = self._zip([("CON.txt", b"x")])
        with self.assertRaises(UnsafeArchiveMember):
            safe_extract(src, self.root / "out")

    def test_nothing_is_written_when_one_member_is_bad(self):
        src = self._zip([("good.txt", b"fine"), ("../bad.txt", b"pwned")])
        dest = self.root / "out"
        with self.assertRaises(UnsafeArchiveMember):
            safe_extract(src, dest)
        # All-or-nothing: the good member must not be on disk either.
        self.assertFalse((dest / "good.txt").exists())

    def test_file_count_budget(self):
        src = self._zip([(f"f{i}.txt", b"x") for i in range(20)])
        with self.assertRaises(ArchiveTooLarge):
            safe_extract(src, self.root / "out", max_files=5)

    def test_declared_size_budget(self):
        src = self._zip([("big.txt", b"x" * 5000)])
        with self.assertRaises(ArchiveTooLarge):
            safe_extract(src, self.root / "out", max_total_bytes=1000)


class TestTarExtraction(_TempRootCase):

    def _tar(self, build) -> Path:
        path = self.root / "in.tar"
        with tarfile.open(path, "w") as tf:
            build(tf)
        return path

    @staticmethod
    def _add_bytes(tf: tarfile.TarFile, name: str, data: bytes):
        import io
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    def test_ordinary_tar_extracts(self):
        src = self._tar(lambda tf: (
            self._add_bytes(tf, "a.txt", b"hello"),
            self._add_bytes(tf, "sub/b.txt", b"world"),
        ))
        dest = self.root / "out"
        result = safe_extract(src, dest)
        self.assertEqual(result.files, 2)
        self.assertEqual((dest / "sub" / "b.txt").read_text(), "world")

    def test_tar_dotdot_is_refused(self):
        src = self._tar(lambda tf: self._add_bytes(tf, "../escaped.txt", b"pwned"))
        with self.assertRaises(UnsafeArchiveMember):
            safe_extract(src, self.root / "out")
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_tar_absolute_is_refused(self):
        src = self._tar(lambda tf: self._add_bytes(tf, "/tmp/evil", b"pwned"))
        with self.assertRaises(UnsafeArchiveMember):
            safe_extract(src, self.root / "out")

    def test_tar_symlink_is_refused(self):
        def build(tf):
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
        src = self._tar(build)
        with self.assertRaises(UnsafeArchiveMember) as ctx:
            safe_extract(src, self.root / "out")
        self.assertIn("link", str(ctx.exception))

    def test_tar_hardlink_is_refused(self):
        def build(tf):
            self._add_bytes(tf, "real.txt", b"x")
            info = tarfile.TarInfo("hard")
            info.type = tarfile.LNKTYPE
            info.linkname = "real.txt"
            tf.addfile(info)
        src = self._tar(build)
        with self.assertRaises(UnsafeArchiveMember):
            safe_extract(src, self.root / "out")

    def test_tar_device_node_is_refused(self):
        def build(tf):
            info = tarfile.TarInfo("dev")
            info.type = tarfile.CHRTYPE
            info.devmajor, info.devminor = 1, 3
            tf.addfile(info)
        src = self._tar(build)
        with self.assertRaises(UnsafeArchiveMember):
            safe_extract(src, self.root / "out")

    def test_setuid_bit_is_not_preserved(self):
        def build(tf):
            import io
            info = tarfile.TarInfo("suid")
            info.size = 1
            info.mode = 0o4755
            tf.addfile(info, io.BytesIO(b"x"))
        src = self._tar(build)
        dest = self.root / "out"
        safe_extract(src, dest)
        mode = (dest / "suid").stat().st_mode
        self.assertFalse(mode & 0o4000, "setuid bit survived extraction")

    def test_lying_header_is_caught_while_writing(self):
        # Declares one byte, delivers far more. Only the write knows.
        def build(tf):
            import io
            payload = b"x" * 4000
            info = tarfile.TarInfo("liar.txt")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        src = self._tar(build)
        with self.assertRaises(ArchiveTooLarge):
            safe_extract(src, self.root / "out", max_total_bytes=100)


class TestUnsupportedArchives(_TempRootCase):

    def test_missing_archive(self):
        with self.assertRaises(FileNotFoundError):
            safe_extract(self.root / "nope.zip", self.root / "out")

    def test_not_an_archive(self):
        src = self.root / "plain.txt"
        src.write_text("not an archive", encoding="utf-8")
        with self.assertRaises(ValueError):
            safe_extract(src, self.root / "out")


if __name__ == "__main__":
    unittest.main()
