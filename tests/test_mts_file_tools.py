"""Offline regression coverage for the MCP file read/write/edit tools."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.request import Request, urlopen

_REQUESTED_ROOT = os.path.join(tempfile.gettempdir(), "mts_file_tools_sandbox")
os.makedirs(_REQUESTED_ROOT, exist_ok=True)
os.environ.setdefault("MCP_SANDBOX_ROOT", _REQUESTED_ROOT)
os.environ.setdefault("MCP_LOG_LEVEL", "ERROR")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mts  # noqa: E402


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="file-tools-", dir=mts.SANDBOX_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.relative = os.path.relpath(self.root, mts.SANDBOX_ROOT)

    def path(self, name="sample.txt"):
        return str(Path(self.relative, name))

    def put(self, data, name="sample.txt"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def call(self, tool, **args):
        return json.loads(mts.call_tool(tool, args))

    def test_new_tools_are_registered_and_described(self):
        listed = self.call("list_tools", category="files")
        self.assertIn("read_lines", mts.TOOL_MAP)
        self.assertIn("replace_lines", mts.TOOL_MAP)
        self.assertIn("read_lines", json.dumps(listed))
        self.assertIn("replace_lines", json.dumps(listed))
        schema = mts._build_mcp_input_schema(mts.TOOL_SCHEMAS["replace_lines"], "replace_lines")
        self.assertEqual(schema["required"], ["filepath", "start_line", "end_line", "content"])
        self.assertIn("inclusive", schema["properties"]["end_line"]["description"])
        self.assertIn("expected_content", schema["properties"])
        self.assertEqual(schema["properties"]["start_line"]["minimum"], 1)
        help_result = self.call("tool_help", name="read_lines")
        self.assertEqual(help_result["example"]["args"]["start_line"], 1)
        self.assertEqual(help_result["example"]["args"]["end_line"], 1)
        self.assertTrue(help_result["input_schema"])

    def test_read_inclusive_numbered_lines_and_preserve_endings(self):
        self.put("α\r\n\r\nthird\rlast")
        result = self.call("read_lines", filepath=self.path(), start_line=2, end_line=3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "\r\nthird\r")
        self.assertEqual(result["lines"], [
            {"line_number": 2, "content": "\r\n"},
            {"line_number": 3, "content": "third\r"},
        ])
        self.assertEqual(result["total_lines"], 4)
        self.assertEqual(result["returned_end_line"], 3)
        self.assertEqual(result["next_start_line"], 4)
        self.assertTrue(result["has_more"])
        self.assertFalse(result["content_truncated"])

        normal = self.call("read_file", filepath=self.path(), start_line=1, line_count=3)
        self.assertEqual(normal["content"], "α\n\nthird\n")
        exact = self.call("read_file", filepath=self.path(), start_line=1, line_count=3,
                          preserve_line_endings=True)
        self.assertEqual(exact["content"], "α\r\n\r\nthird\r")
        end = self.call("read_lines", filepath=self.path(), start_line=4, end_line=9)
        self.assertEqual(end["content"], "last")
        self.assertFalse(end["has_more"])
        self.assertIsNone(end["next_start_line"])
        beyond = self.call("read_lines", filepath=self.path(), start_line=6, end_line=6)
        self.assertEqual(beyond["lines"], [])
        self.assertIsNone(beyond["returned_end_line"])
        self.assertFalse(beyond["has_more"])

    def test_unicode_separators_do_not_create_physical_lines(self):
        path = self.put("first\u2028still-first\nsecond\n")
        read = self.call("read_lines", filepath=self.path(), start_line=1, end_line=1)
        self.assertEqual(read["content"], "first\u2028still-first\n")
        self.assertEqual(read["total_lines"], 2)
        self.call("replace_lines", filepath=self.path(), start_line=2, end_line=2, content="new")
        self.assertEqual(path.read_text(encoding="utf-8"), "first\u2028still-first\nnew\n")

    def test_paging_and_partial_line_metadata(self):
        self.put("abc\nnext\n")
        with mock.patch.object(mts, "MAX_TEXT_CHARS", 4):
            page = self.call("read_lines", filepath=self.path(), start_line=1, end_line=2)
            self.assertEqual(page["content"], "abc\n")
            self.assertTrue(page["content_truncated"])
            self.assertFalse(page["partial_line"])
            self.assertEqual(page["next_start_line"], 2)
            self.assertTrue(page["has_more"])
            long_line = self.call("read_lines", filepath=self.path(), start_line=2, end_line=2)
            self.assertEqual(long_line["content"], "next")
            self.assertTrue(long_line["partial_line"])
            self.assertTrue(long_line["has_more"])
            self.assertIsNone(long_line["next_start_line"])
        self.put("long-unbroken-line")
        with mock.patch.object(mts, "MAX_TEXT_CHARS", 4):
            only_line = self.call("read_file", filepath=self.path())
            self.assertEqual(only_line["total_lines"], 1)
            self.assertTrue(only_line["content_truncated"])
            self.assertTrue(only_line["has_more"])

    def test_skipping_line_handles_crlf_split_at_chunk_boundary(self):
        self.put(b"abcd\r\nb\r\n")
        with mock.patch.object(mts, "MAX_TEXT_CHARS", 4):
            result = self.call("read_lines", filepath=self.path(), start_line=2, end_line=2)
        self.assertEqual(result["content"], "b\r\n")
        self.assertEqual(result["lines"][0]["line_number"], 2)
        self.assertFalse(result["has_more"])

    def test_large_file_reads_report_unknown_total_and_correct_eof(self):
        self.put("1\n2\n3\n")
        with mock.patch.object(mts, "MAX_FILE_SCAN_BYTES", 4):
            first = self.call("read_file", filepath=self.path(), line_count=1)
            self.assertFalse(first["total_lines_known"])
            self.assertIsNone(first["total_lines"])
            self.assertTrue(first["has_more"])
            all_lines = self.call("read_file", filepath=self.path(), line_count=10)
            self.assertFalse(all_lines["has_more"])
            too_far = self.call("read_lines", filepath=self.path(), start_line=4, end_line=4)
            self.assertIn("scanning", too_far["error"])

    def test_skipped_scan_limit_counts_physical_crlf_bytes(self):
        self.put(b"a\r\nb\r\nc\r\n")
        with mock.patch.object(mts, "MAX_FILE_SCAN_BYTES", 5):
            result = self.call("read_file", filepath=self.path(), start_line=3, line_count=1)
        self.assertFalse(result["ok"])
        self.assertIn("scanning", result["error"])

    def test_read_ranges_validate_bounds_and_size(self):
        self.put("one\ntwo\n")
        for start, end in ((0, 1), (-1, 1), (2, 1), (1, 20001)):
            with self.subTest(start=start, end=end):
                result = self.call("read_lines", filepath=self.path(), start_line=start, end_line=end)
                self.assertFalse(result["ok"])
        self.assertFalse(self.call("read_lines", filepath=self.path(), start_line="bad", end_line=1)["ok"])
        self.assertFalse(self.call("read_lines", filepath=self.path(), start_line=True, end_line=1)["ok"])
        self.assertFalse(self.call("read_lines", filepath=self.path(), start_line=1)["ok"])

    def test_replace_range_roundtrip_backup_newlines_and_mode(self):
        path = self.put("one\r\ntwo\r\nthree\r\nfour")
        path.chmod(0o755)
        read = self.call("read_lines", filepath=self.path(), start_line=2, end_line=3)
        result = self.call("replace_lines", filepath=self.path(), start_line=2, end_line=3,
                           content="新\nlines", expected_content=read["content"], create_backup=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["lines_replaced"], 2)
        self.assertEqual(result["lines_inserted"], 2)
        self.assertEqual(path.read_bytes(), "one\r\n新\r\nlines\r\nfour".encode())
        self.assertEqual(Path(result["backup"]).read_bytes(), b"one\r\ntwo\r\nthree\r\nfour")
        self.assertEqual(result["bytes_written"], len(path.read_bytes()))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(Path(result["backup"]).stat().st_mode), 0o755)

    def test_replace_at_eof_preserves_termination_and_explicit_newlines(self):
        path = self.put("first\nlast")
        result = self.call("replace_lines", filepath=self.path(), start_line=2, end_line=2,
                           content="replacement")
        self.assertTrue(result["ok"])
        self.assertEqual(path.read_bytes(), b"first\nreplacement")
        self.call("replace_lines", filepath=self.path(), start_line=2, end_line=2, content="more\n")
        self.assertEqual(path.read_bytes(), b"first\nmore\n")
        self.call("replace_lines", filepath=self.path(), start_line=2, end_line=2, content="again")
        self.assertEqual(path.read_bytes(), b"first\nagain\n")
        path.write_bytes(b"first\rsecond\rlast")
        self.call("replace_lines", filepath=self.path(), start_line=2, end_line=2, content="A\nB")
        self.assertEqual(path.read_bytes(), b"first\rA\rB\rlast")

    def test_empty_replacement_deletes_selected_lines(self):
        path = self.put("one\ntwo\nthree\n")
        result = self.call("replace_lines", filepath=self.path(), start_line=2, end_line=2, content="")
        self.assertEqual(result["lines_inserted"], 0)
        self.assertEqual(path.read_bytes(), b"one\nthree\n")
        self.call("replace_lines", filepath=self.path(), start_line=1, end_line=2, content="")
        self.assertEqual(path.read_bytes(), b"")
        empty = self.call("replace_lines", filepath=self.path(), start_line=1, end_line=1, content="x")
        self.assertFalse(empty["ok"])
        self.assertEqual(path.read_bytes(), b"")

    def test_first_line_replacement_preserves_utf8_bom(self):
        path = self.put(b"\xef\xbb\xbffirst\r\nsecond\r\n")
        original = self.call("read_lines", filepath=self.path(), start_line=1, end_line=1)
        self.assertEqual(original["content"], "\ufefffirst\r\n")
        changed = self.call("replace_lines", filepath=self.path(), start_line=1, end_line=1,
                            content="new", expected_content=original["content"])
        self.assertEqual(changed["lines_inserted"], 1)
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbfnew\r\nsecond\r\n")
        deleted = self.call("replace_lines", filepath=self.path(), start_line=1, end_line=1, content="")
        self.assertEqual(deleted["lines_inserted"], 0)
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbfsecond\r\n")
        self.call("replace_lines", filepath=self.path(), start_line=1, end_line=1, content="")
        self.assertEqual(path.read_bytes(), b"")

    def test_invalid_replacement_ranges_never_modify_or_create_backups(self):
        path = self.put("one\ntwo\n")
        for start, end in ((0, 1), (2, 1), (3, 3), (1, 3)):
            with self.subTest(start=start, end=end):
                result = self.call("replace_lines", filepath=self.path(), start_line=start, end_line=end,
                                   content="changed", create_backup=True)
                self.assertFalse(result["ok"])
                self.assertEqual(path.read_bytes(), b"one\ntwo\n")
                self.assertFalse(Path(str(path) + ".bak").exists())

    def test_expected_content_prevents_stale_edits(self):
        path = self.put("one\ntwo\nthree\n")
        read = self.call("read_lines", filepath=self.path(), start_line=2, end_line=2)
        path.write_bytes(b"one\nupdated\nthree\n")
        result = self.call("replace_lines", filepath=self.path(), start_line=2, end_line=2,
                           content="new", expected_content=read["content"], create_backup=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["conflict"])
        self.assertEqual(path.read_bytes(), b"one\nupdated\nthree\n")
        self.assertFalse(Path(str(path) + ".bak").exists())

    def test_dry_run_previews_without_writes_or_backups(self):
        path = self.put("one\r\ntwo\r\n")
        original_stat = path.stat()
        preview = self.call("replace_lines", filepath=self.path(), start_line=2, end_line=2,
                            content="new\nnext", dry_run=True, create_backup=True)
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(preview["bytes_written"], 0)
        self.assertEqual(preview["original_content"], "two\r\n")
        self.assertEqual(preview["replacement_content"], "new\r\nnext\r\n")
        self.assertEqual(path.read_bytes(), b"one\r\ntwo\r\n")
        self.assertEqual(path.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertFalse(Path(str(path) + ".bak").exists())
        snippet = self.call("edit_file_replace", filepath=self.path(), target_snippet="two",
                            replacement_snippet="TWO", dry_run=True, create_backup=True)
        self.assertTrue(snippet["ok"])
        self.assertEqual(snippet["bytes_written"], 0)
        self.assertEqual(snippet["original_snippet"], "two")
        self.assertEqual(snippet["replacement_snippet"], "TWO")
        self.assertEqual(path.read_bytes(), b"one\r\ntwo\r\n")
        self.assertFalse(Path(str(path) + ".bak").exists())

    def test_write_create_only_backup_and_permissions(self):
        path = self.put("original")
        path.chmod(0o751)
        denied = self.call("write_file", filepath=self.path(), content="new", overwrite=False,
                           create_backup=True)
        self.assertFalse(denied["ok"])
        self.assertEqual(path.read_bytes(), b"original")
        self.assertFalse(Path(str(path) + ".bak").exists())
        result = self.call("write_file", filepath=self.path(), content="new", create_backup=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["previous_bytes"], 8)
        self.assertEqual(path.read_bytes(), b"new")
        self.assertEqual(Path(result["backup"]).read_bytes(), b"original")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o751)
        created = self.call("write_file", filepath=self.path("sub/new.txt"), content="text",
                            overwrite=False, create_backup=True)
        self.assertTrue(created["created"])
        self.assertEqual(created["backup"], "")
        self.assertEqual((self.root / "sub/new.txt").read_bytes(), b"text")

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits required")
    def test_full_patch_preserves_ordinary_permissions_not_setuid(self):
        path = self.put("original\n")
        path.chmod(0o4755)
        result = self.call("apply_patch", filepath=self.path(), patch="replacement\n", mode="replace")
        self.assertTrue(result["ok"])
        self.assertEqual(path.read_bytes(), b"replacement\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_internal_atomic_writes_still_default_to_private_permissions(self):
        path = self.put("internal")
        path.chmod(0o644)
        mts._atomic_write(str(path), "updated")
        self.assertEqual(path.read_bytes(), b"updated")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_write_create_only_is_atomic_against_a_concurrent_creator(self):
        destination = self.root / "race.txt"
        link = os.link

        def competing_create(src, dst):
            Path(dst).write_bytes(b"competitor")
            return link(src, dst)

        with mock.patch.object(mts.os, "link", side_effect=competing_create):
            result = self.call("write_file", filepath=self.path("race.txt"), content="ours", overwrite=False)
        self.assertFalse(result["ok"])
        self.assertEqual(destination.read_bytes(), b"competitor")
        self.assertFalse(list(self.root.glob(".mts-*")))

    def test_append_uses_the_existing_newline_style_for_separator(self):
        path = self.put("one\r\ntwo")
        result = self.call("append_to_file", filepath=self.path(), content="three")
        self.assertTrue(result["added_separator_newline"])
        self.assertEqual(path.read_bytes(), b"one\r\ntwo\r\nthree")
        self.call("append_to_file", filepath=self.path(), content="\r\nfour")
        self.assertEqual(path.read_bytes(), b"one\r\ntwo\r\nthree\r\nfour")

    def test_edits_reject_binary_and_invalid_utf8_without_modifying_them(self):
        for raw in (b"one\xfftwo\n", b"one\x00two\n"):
            with self.subTest(raw=raw):
                path = self.put(raw)
                line_edit = self.call("replace_lines", filepath=self.path(), start_line=1, end_line=1,
                                      content="changed", create_backup=True)
                snippet_edit = self.call("edit_file_replace", filepath=self.path(), target_snippet="one",
                                         replacement_snippet="changed", create_backup=True)
                self.assertFalse(line_edit["ok"])
                self.assertFalse(snippet_edit["ok"])
                self.assertEqual(path.read_bytes(), raw)
                self.assertFalse(Path(str(path) + ".bak").exists())

    def test_exact_snippet_edit_preserves_unmodified_bytes_and_mode(self):
        path = self.put("one\r\nitem\r\nitem\r\n")
        path.chmod(0o755)
        ambiguous = self.call("edit_file_replace", filepath=self.path(), target_snippet="item",
                              replacement_snippet="new")
        self.assertEqual(ambiguous["occurrences"], 2)
        self.assertEqual(path.read_bytes(), b"one\r\nitem\r\nitem\r\n")
        result = self.call("edit_file_replace", filepath=self.path(), target_snippet="item",
                           replacement_snippet="new", replace_all=True, create_backup=True)
        self.assertEqual(result["match_mode"], "exact")
        self.assertEqual(result["replacements_made"], 2)
        self.assertEqual(path.read_bytes(), b"one\r\nnew\r\nnew\r\n")
        self.assertEqual(Path(result["backup"]).read_bytes(), b"one\r\nitem\r\nitem\r\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_flexible_matches_preserve_each_indent_and_eof_ending(self):
        path = self.put("  alpha\r\n  beta\r\n--\n    alpha\n    beta")
        args = dict(filepath=self.path(), target_snippet="alpha\nbeta", replacement_snippet="new")
        ambiguous = self.call("edit_file_replace", **args)
        self.assertEqual(ambiguous["occurrences"], 2)
        preview = self.call("edit_file_replace", **args, replace_all=True, dry_run=True)
        self.assertEqual(preview["original_snippet"], "  alpha\r\n  beta\r\n")
        self.assertEqual(preview["replacement_snippet"], "  new\r\n")
        result = self.call("edit_file_replace", **args, replace_all=True)
        self.assertEqual(result["match_mode"], "whitespace-normalized")
        self.assertEqual(result["replacements_made"], 2)
        self.assertEqual(path.read_bytes(), b"  new\r\n--\n    new")

    def test_flexible_first_line_matching_preserves_bom(self):
        path = self.put(b"\xef\xbb\xbf  alpha\r\n  beta\r\nkeep\r\n")
        args = dict(filepath=self.path(), target_snippet="alpha\nbeta", replacement_snippet="new")
        preview = self.call("edit_file_replace", **args, dry_run=True)
        self.assertEqual(preview["match_mode"], "whitespace-normalized")
        self.assertEqual(preview["replacement_snippet"], "\ufeff  new\r\n")
        self.assertTrue(self.call("edit_file_replace", **args)["ok"])
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbf  new\r\nkeep\r\n")
        path.write_bytes(b"\xef\xbb\xbf  alpha\n  beta\nkeep\n")
        self.call("edit_file_replace", filepath=self.path(), target_snippet="alpha\nbeta",
                  replacement_snippet="")
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbfkeep\n")

    def test_flexible_matching_preserves_unicode_indentation(self):
        path = self.put("\u00a0\u00a0alpha\n\u00a0\u00a0beta\n")
        result = self.call("edit_file_replace", filepath=self.path(), target_snippet="alpha\nbeta",
                           replacement_snippet="new")
        self.assertEqual(result["match_mode"], "whitespace-normalized")
        self.assertEqual(path.read_text(encoding="utf-8"), "\u00a0\u00a0new\n")

    def test_flexible_delete_and_overlapping_matches(self):
        path = self.put("  alpha\n  beta\nkeep\n")
        result = self.call("edit_file_replace", filepath=self.path(), target_snippet="alpha\nbeta",
                           replacement_snippet="")
        self.assertTrue(result["ok"])
        self.assertEqual(path.read_bytes(), b"keep\n")
        path.write_bytes(b" a\n a\n a\n")
        result = self.call("edit_file_replace", filepath=self.path(), target_snippet="a\na",
                           replacement_snippet="b", replace_all=True)
        self.assertEqual(result["replacements_made"], 1)
        self.assertEqual(path.read_bytes(), b" b\n a\n")

    def test_output_and_input_size_limits_preserve_original(self):
        path = self.put("a\na\n")
        with mock.patch.object(mts, "_MAX_EDIT_OUTPUT_BYTES", 8):
            exact = self.call("edit_file_replace", filepath=self.path(), target_snippet="a",
                              replacement_snippet="123456789", replace_all=True, create_backup=True)
            line = self.call("replace_lines", filepath=self.path(), start_line=1, end_line=1,
                             content="123456789", create_backup=True)
        self.assertFalse(exact["ok"])
        self.assertFalse(line["ok"])
        self.assertEqual(path.read_bytes(), b"a\na\n")
        self.assertFalse(Path(str(path) + ".bak").exists())
        with mock.patch.object(mts, "MAX_FILE_SCAN_BYTES", 3):
            too_large = self.call("replace_lines", filepath=self.path(), start_line=1, end_line=1,
                                  content="x")
        self.assertFalse(too_large["ok"])
        self.assertEqual(path.read_bytes(), b"a\na\n")

    def test_atomic_write_failure_keeps_original_and_cleans_temp(self):
        path = self.put("original\n")
        with mock.patch.object(mts.os, "replace", side_effect=OSError("simulated failure")):
            result = self.call("replace_lines", filepath=self.path(), start_line=1, end_line=1,
                               content="new")
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_bytes(), b"original\n")
        self.assertFalse(list(self.root.glob(".mts-*")))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_target_and_backup_symlinks_cannot_escape_sandbox(self):
        path = self.put("original\n")
        with tempfile.TemporaryDirectory() as outside:
            victim = Path(outside, "victim.txt")
            victim.write_bytes(b"private\n")
            try:
                os.symlink(victim, self.root / "outside.txt")
                os.symlink(victim, Path(str(path) + ".bak"))
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            read = self.call("read_lines", filepath=self.path("outside.txt"), start_line=1, end_line=1)
            edit = self.call("replace_lines", filepath=self.path("outside.txt"), start_line=1, end_line=1,
                             content="bad")
            backup = self.call("replace_lines", filepath=self.path(), start_line=1, end_line=1,
                               content="new", create_backup=True)
            self.assertFalse(read["ok"])
            self.assertFalse(edit["ok"])
            self.assertFalse(backup["ok"])
            self.assertEqual(victim.read_bytes(), b"private\n")
            self.assertEqual(path.read_bytes(), b"original\n")

    def test_cli_exposes_line_tools(self):
        self.put("first\nsecond\n")
        env = dict(os.environ, MCP_SANDBOX_ROOT=mts.SANDBOX_ROOT, MCP_LOG_LEVEL="ERROR")
        completed = subprocess.run(
            [sys.executable, str(Path(mts.__file__)), "--call", "read_lines",
             json.dumps({"filepath": self.path(), "start_line": 2, "end_line": 2})],
            capture_output=True, text=True, check=True, env=env,
        )
        self.assertEqual(json.loads(completed.stdout)["content"], "second\n")

    def test_jsonrpc_discovery_and_read_replace_workflow(self):
        path = self.put("first\nsecond\nthird\n")
        with mock.patch.object(mts, "REQUIRE_AUTH", False):
            server = ThreadingHTTPServer(("127.0.0.1", 0), mts.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_port}/"

            def rpc(method, params=None):
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                   "params": params or {}}).encode()
                request = Request(url, data=body, headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=3) as response:
                    return json.load(response)["result"]

            tools = {item["name"]: item for item in rpc("tools/list")["tools"]}
            self.assertIn("read_lines", tools)
            self.assertIn("replace_lines", tools)
            self.assertEqual(tools["read_lines"]["inputSchema"]["required"],
                             ["filepath", "start_line", "end_line"])
            read = rpc("tools/call", {"name": "read_lines", "arguments": {
                "filepath": self.path(), "start_line": 2, "end_line": 2}})
            self.assertFalse(read["isError"])
            content = json.loads(read["content"][0]["text"])["content"]
            edited = rpc("tools/call", {"name": "replace_lines", "arguments": {
                "filepath": self.path(), "start_line": 2, "end_line": 2,
                "content": "changed", "expected_content": content}})
            self.assertFalse(edited["isError"])
            self.assertEqual(path.read_bytes(), b"first\nchanged\nthird\n")
            conflict = rpc("tools/call", {"name": "replace_lines", "arguments": {
                "filepath": self.path(), "start_line": 2, "end_line": 2,
                "content": "bad", "expected_content": content}})
            self.assertTrue(conflict["isError"])
            self.assertEqual(path.read_bytes(), b"first\nchanged\nthird\n")


if __name__ == "__main__":
    unittest.main()
