#!/usr/bin/env python3
"""Regression tests for the v4.2 tool upgrade pass (mts.py).

Every test here pins down a behaviour that was either broken, missing or
inconsistent in v4.1. The suite is fully offline: network tools run against a
loopback fixture and git tests use a throwaway repository.

    python3 -m unittest tests.test_mts_upgrades -v
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

_REQUESTED_ROOT = os.path.join(tempfile.gettempdir(), "mts_upgrade_sandbox")
os.makedirs(_REQUESTED_ROOT, exist_ok=True)
os.environ.setdefault("MCP_SANDBOX_ROOT", _REQUESTED_ROOT)
os.environ["MCP_ENABLE_DANGEROUS"] = "true"
os.environ.setdefault("MCP_MEMORY_FILE", os.path.join(_REQUESTED_ROOT, "memory.json"))
os.environ.setdefault("MCP_LOG_LEVEL", "ERROR")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mts  # noqa: E402

# When the whole suite runs in one process, mts may already be bound to another
# sandbox root. Always work inside whatever root the module actually resolved.
SANDBOX = os.path.join(mts.SANDBOX_ROOT, "upgrade-tests")
shutil.rmtree(SANDBOX, ignore_errors=True)
os.makedirs(SANDBOX, exist_ok=True)


def call(tool, **kwargs):
    return json.loads(mts.call_tool(tool, kwargs))


def fresh_dir(name):
    path = os.path.join(SANDBOX, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def sandbox_relative(path):
    """Paths handed to tools are relative to the module's sandbox root, not to SANDBOX."""
    return os.path.relpath(path, mts.SANDBOX_ROOT)


class CsvUpgradeTests(unittest.TestCase):
    def test_semicolon_and_tab_exports_are_detected(self):
        semi = call("csv_to_json", csv_text="a;b\n1;2\n")
        self.assertEqual(semi["delimiter"], ";")
        self.assertTrue(semi["delimiter_autodetected"])
        self.assertEqual(semi["rows"], [{"a": "1", "b": "2"}])

        tab = call("csv_to_json", csv_text="a\tb\n1\t2\n")
        self.assertEqual(tab["delimiter"], "\t")
        self.assertEqual(tab["rows"], [{"a": "1", "b": "2"}])

        auto = call("csv_to_json", csv_text="a|b\n1|2\n", delimiter="auto")
        self.assertEqual(auto["delimiter"], "|")

    def test_bom_is_stripped_from_text_and_files(self):
        inline = call("csv_to_json", csv_text="\ufeffname,value\nalpha,1\n")
        self.assertTrue(inline["had_bom"])
        self.assertEqual(inline["columns"], ["name", "value"])

        root = fresh_dir("csv-bom")
        path = os.path.join(root, "bom.csv")
        with open(path, "w", encoding="utf-8-sig") as handle:
            handle.write("name,value\nalpha,1\n")
        from_file = call("csv_to_json", filepath=sandbox_relative(path))
        self.assertEqual(from_file["columns"], ["name", "value"])

    def test_explicit_non_default_delimiter_is_honoured(self):
        result = call("csv_to_json", csv_text="a,b\n1,2\n", delimiter="\t")
        self.assertEqual(result["delimiter"], "\t")
        self.assertFalse(result["delimiter_autodetected"])

    def test_ragged_and_duplicate_columns_are_reported(self):
        ragged = call("csv_to_json", csv_text="a,b\n1,2,3\n")
        self.assertEqual(ragged["rows"], [{"a": "1", "b": "2", "_extra": ["3"]}])

        duplicate = call("csv_to_json", csv_text="a,a,b\n1,2,3\n")
        self.assertEqual(duplicate["duplicate_columns"], ["a"])

    def test_json_to_csv_reports_size_and_delimiter(self):
        result = call("json_to_csv", json_string='[{"a":1,"b":2}]', delimiter=";")
        self.assertEqual(result["delimiter"], ";")
        self.assertIn("a;b", result["csv"])
        self.assertEqual(result["bytes"], len(result["csv"].encode("utf-8")))


class TextUpgradeTests(unittest.TestCase):
    def test_readability_uses_a_consistent_basis(self):
        text = "alpha beta gamma delta. " * 30000
        stats = call("text_stats", text=text)
        self.assertLessEqual(stats["flesch_reading_ease"], 121.0)
        self.assertGreater(stats["flesch_reading_ease"], 0.0)
        self.assertTrue(stats["readability_estimated"])
        self.assertEqual(stats["readability_sampled_words"], 100_000)
        self.assertIn("reading_level", stats)

    def test_unicode_words_are_counted(self):
        stats = call("text_stats", text="Naïve café don't stop. 日本語 too!")
        self.assertEqual(stats["words"], 6)
        self.assertEqual(stats["sentences"], 2)

    def test_diff_can_ignore_whitespace_and_case(self):
        plain = call("text_diff_compare", text1="Alpha\nBeta\n", text2="alpha\nBeta \n")
        self.assertFalse(plain["identical"])
        relaxed = call("text_diff_compare", text1="Alpha\nBeta\n", text2="alpha\nBeta \n",
                       ignore_case=True, ignore_whitespace=True)
        self.assertTrue(relaxed["identical"])
        self.assertEqual(relaxed["lines_added"], 0)


class EncodingUpgradeTests(unittest.TestCase):
    def test_url_decode_honours_component_flag(self):
        component = call("url_encode_decode", text="a+b%20c", mode="decode", component=True)
        self.assertEqual(component["result"], "a b c")
        whole = call("url_encode_decode", text="a+b%20c", mode="decode", component=False)
        self.assertEqual(whole["result"], "a+b c")

    def test_base64_accepts_line_wrapped_pem_style_input(self):
        encoded = base64.b64encode(b"hello world").decode()
        wrapped = encoded[:8] + "\r\n" + encoded[8:] + "\n"
        result = call("base64_encode_decode", text=wrapped, mode="decode")
        self.assertEqual(result["result"], "hello world")
        self.assertGreater(result["line_breaks_stripped"], 0)
        # Interior spaces remain invalid input.
        self.assertIn("error", call("base64_encode_decode", text="YW Jj", mode="decode"))

    def test_hash_supports_shake_length_and_rejects_shake_hmac(self):
        result = call("hash_text", text="abc", algorithm="shake_128", length=64)
        self.assertEqual(result["digest_bytes"], 64)
        self.assertEqual(len(result["digest"]), 128)
        self.assertTrue(result["extendable_output"])
        self.assertIn("error", call("hash_text", text="abc", algorithm="shake_128",
                                    hmac_key="secret"))


class JwtUpgradeTests(unittest.TestCase):
    @staticmethod
    def token(header, payload):
        def segment(value):
            raw = json.dumps(value).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"{segment(header)}.{segment(payload)}."

    def test_out_of_range_claims_do_not_break_decoding(self):
        result = call("jwt_decode", token=self.token({"alg": "RS256"}, {"exp": 10 ** 30, "sub": "x"}))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["exp_iso"])
        self.assertIn("outside the supported date range", result["exp_note"])
        self.assertTrue(any("'exp' claim" in warning for warning in result["warnings"]))

    def test_time_claims_and_alg_none_are_reported(self):
        expired = call("jwt_decode", token=self.token(
            {"alg": "HS256"}, {"exp": 1_000_000_000, "iat": 1_000_000_000, "nbf": 1_000_000_000}))
        self.assertTrue(expired["expired"])
        self.assertFalse(expired["not_yet_valid"])
        self.assertEqual(expired["algorithm"], "HS256")

        unsigned = call("jwt_decode", token=self.token({"alg": "none"}, {"sub": "x"}))
        self.assertTrue(unsigned["alg_none"])
        self.assertFalse(unsigned["signature_present"])
        self.assertTrue(any("none" in warning for warning in unsigned["warnings"]))


class GeneratorUpgradeTests(unittest.TestCase):
    def test_uuid_namespace_selection_is_deterministic(self):
        dns = call("uuid_generate", version=5, namespace_name="example.test")
        url = call("uuid_generate", version=5, namespace_name="example.test", namespace="url")
        again = call("uuid_generate", version=5, namespace_name="example.test")
        self.assertEqual(dns["uuid"], again["uuid"])
        self.assertNotEqual(dns["uuid"], url["uuid"])
        self.assertEqual(dns["namespace"], "dns")
        literal = call("uuid_generate", version=5, namespace_name="example.test",
                       namespace="6ba7b810-9dad-11d1-80b4-00c04fd430c8")
        self.assertEqual(literal["uuid"], dns["uuid"])
        self.assertIn("error", call("uuid_generate", version=5, namespace_name="x",
                                    namespace="not a namespace"))
        self.assertIn("error", call("uuid_generate", version=3))

    def test_random_string_excludes_ambiguous_characters(self):
        result = call("random_string", length=256, charset="alphanumeric",
                      exclude_ambiguous=True)
        self.assertFalse(set(result["value"]) & set("0Oo1lI5S2Z8B"))
        self.assertTrue(result["excluded_ambiguous"])

    def test_password_preset_always_contains_every_class(self):
        result = call("random_string", length=8, charset="password", count=20)
        for value in result["values"]:
            self.assertTrue(any(ch.islower() for ch in value), value)
            self.assertTrue(any(ch.isupper() for ch in value), value)
            self.assertTrue(any(ch.isdigit() for ch in value), value)
            self.assertTrue(any(not ch.isalnum() for ch in value), value)
            self.assertEqual(len(value), 8)


class TimeUpgradeTests(unittest.TestCase):
    def test_named_timezone_is_supported(self):
        result = call("current_datetime", timezone="America/New_York")
        self.assertEqual(result["timezone"], "America/New_York")
        self.assertIn(result["utc_offset"], ("-0400", "-0500"))
        self.assertIsInstance(result["dst"], bool)
        self.assertIn("iso_week", result)

    def test_unknown_timezone_and_bad_offset_fail_clearly(self):
        self.assertIn("Unknown timezone", call("current_datetime", timezone="Mars/Base")["error"])
        self.assertIn("error", call("current_datetime", timezone_offset_hours=float("inf")))

    def test_timestamp_units_are_explicit_and_auto(self):
        seconds = call("timestamp_convert", value="1704067200")["iso"]
        self.assertEqual(call("timestamp_convert", value="1704067200000")["iso"], seconds)
        self.assertEqual(call("timestamp_convert", value="1704067200000000")["iso"], seconds)
        self.assertEqual(call("timestamp_convert", value="1704067200", unit="ms")["iso"],
                         "1970-01-20T17:21:07.200000+00:00")
        self.assertEqual(call("timestamp_convert", value="2024-01-01T00:00:00Z",
                              to_format="rfc2822")["result"],
                         "Mon, 01 Jan 2024 00:00:00 +0000")
        self.assertIn("error", call("timestamp_convert", value="0", unit="fortnights"))
        self.assertIn("age_human", call("timestamp_convert", value="1704067200"))


class FileSystemUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.root = fresh_dir("fs-upgrades")
        self.relative = sandbox_relative(self.root)

    def test_append_never_glues_records_together(self):
        first = call("write_file", filepath=f"{self.relative}/log.txt", content="line-one")
        self.assertTrue(first["created"])
        appended = call("append_to_file", filepath=f"{self.relative}/log.txt", content="line-two")
        self.assertTrue(appended["added_separator_newline"])
        with open(os.path.join(self.root, "log.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "line-one\nline-two")

        # A caller that supplies its own leading newline must not get a second one.
        terminated = call("append_to_file", filepath=f"{self.relative}/log.txt",
                          content="\nline-three")
        self.assertFalse(terminated["added_separator_newline"])

        raw = call("append_to_file", filepath=f"{self.relative}/raw.txt", content="a",
                   ensure_newline=False)
        self.assertFalse(raw["added_separator_newline"])

    def test_write_file_reports_creation_and_previous_size(self):
        path = f"{self.relative}/meta.txt"
        created = call("write_file", filepath=path, content="12345")
        self.assertTrue(created["created"])
        updated = call("write_file", filepath=path, content="1")
        self.assertFalse(updated["created"])
        self.assertEqual(updated["previous_bytes"], 5)

    def test_edit_tolerates_indentation_drift_without_breaking_code(self):
        path = os.path.join(self.root, "code.py")
        source = "def f():\n    if True:\n        return 1\n    else:\n        return 2\n"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        result = call("edit_file_replace", filepath=f"{self.relative}/code.py",
                      target_snippet="if True:\n    return 1\nelse:\n    return 2",
                      replacement_snippet="if True:\n        return 3")
        self.assertEqual(result["match_mode"], "whitespace-normalized")
        with open(path, encoding="utf-8") as handle:
            updated = handle.read()
        self.assertEqual(updated, "def f():\n    if True:\n        return 3\n")
        compile(updated, "code.py", "exec")  # must still be valid Python

        disabled = call("edit_file_replace", filepath=f"{self.relative}/code.py",
                        target_snippet="if True:\nreturn 3", replacement_snippet="pass",
                        whitespace_flexible=False)
        self.assertIn("error", disabled)

    def test_list_directory_validates_and_sorts(self):
        for name in ("z.txt", "a.txt", "m.md"):
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        default = call("list_directory", path=self.relative)
        self.assertEqual([item["name"] for item in default["items"]],
                         ["a.txt", "m.md", "z.txt"])
        self.assertEqual(default["sort_by"], "name")
        self.assertEqual([item["name"] for item in
                          call("list_directory", path=self.relative, sort_by="name_desc")["items"]],
                         ["z.txt", "m.md", "a.txt"])
        self.assertIn("Unsupported sort_by", call("list_directory", path=self.relative,
                                                  sort_by="bogus")["error"])

    def test_checksum_can_verify_an_expected_digest(self):
        path = f"{self.relative}/verify.txt"
        call("write_file", filepath=path, content="payload")
        digest = call("file_checksum", filepath=path)["checksum"]
        self.assertTrue(call("file_checksum", filepath=path, expected=digest.upper())["matches"])
        self.assertFalse(call("file_checksum", filepath=path, expected="deadbeef")["matches"])

    def test_disk_usage_declares_partial_totals(self):
        os.makedirs(os.path.join(self.root, ".git", "objects"), exist_ok=True)
        with open(os.path.join(self.root, ".git", "objects", "pack"), "w") as handle:
            handle.write("x" * 64)
        result = call("disk_usage", path=self.relative)
        self.assertFalse(result["total_size_complete"])
        self.assertEqual(result["excluded_directories"]["names"], [".git"])

    def test_search_files_matches_relative_paths(self):
        os.makedirs(os.path.join(self.root, "pkg"), exist_ok=True)
        with open(os.path.join(self.root, "pkg", "mod.py"), "w") as handle:
            handle.write("x")
        with open(os.path.join(self.root, "top.py"), "w", encoding="utf-8") as handle:
            handle.write("x")
        nested = call("search_files", directory=self.relative, pattern="pkg/*.py")
        self.assertEqual([item["name"] for item in nested["files"]], ["mod.py"])
        flat = call("search_files", directory=self.relative, pattern="*.py")
        self.assertIn("mod.py", [item["name"] for item in flat["files"]])


class ProcessUpgradeTests(unittest.TestCase):
    def test_process_list_returns_the_heaviest_consumers(self):
        ps_output = "\n".join([
            "  PID COMMAND USER %CPU %MEM STAT",
            " 1 light root 1.0 0.5 S",
            " 2 heavy root 2.0 50.0 S",
            " 3 medium root 0.5 9.9 S",
        ])
        with mock.patch.object(mts, "HAS_PSUTIL", False), \
                mock.patch.object(mts, "shutil") as fake_shutil, \
                mock.patch.object(mts, "_run_subprocess") as runner:
            fake_shutil.which.return_value = "/usr/bin/ps"
            runner.return_value = {"exit_code": 0, "stdout": ps_output, "stderr": "",
                                   "stdout_truncated": False, "stderr_truncated": False}
            result = call("process_list", max_results=2)
        self.assertEqual([p["command"] for p in result["processes"]], ["heavy", "medium"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["sorted_by"], "memory_percent")


class RobotsUpgradeTests(unittest.TestCase):
    RULES = ("User-agent: *\n"
             "Disallow: /private\n"
             "Allow: /private/ok\n"
             "Disallow: /*.pdf$\n")

    def test_rules_are_evaluated_for_a_url(self):
        denied = call("parse_robots_txt", text=self.RULES, url="https://example.test/private/x")
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["matched_rule"], "/private")

        allowed = call("parse_robots_txt", text=self.RULES, url="https://example.test/private/ok/x")
        self.assertTrue(allowed["allowed"])
        self.assertEqual(allowed["matched_rule"], "/private/ok")

        wildcard = call("parse_robots_txt", text=self.RULES, url="https://example.test/a/b.pdf")
        self.assertFalse(wildcard["allowed"])
        self.assertEqual(wildcard["matched_rule"], "/*.pdf$")

        open_path = call("parse_robots_txt", text=self.RULES, url="https://example.test/public")
        self.assertTrue(open_path["allowed"])


class RegexReplaceTests(unittest.TestCase):
    def test_substitution_counts_and_backreferences(self):
        result = call("regex_replace", pattern=r"(\w+)@(\w+)", replacement=r"\2:\1",
                      text="a@b c@d", max_replacements=1)
        self.assertEqual(result["result"], "b:a c@d")
        self.assertEqual(result["replacements"], 1)
        self.assertTrue(result["changed"])

        everything = call("regex_replace", pattern="a", replacement="X", text="aaa")
        self.assertEqual(everything["result"], "XXX")
        self.assertEqual(everything["replacements"], 3)

    def test_invalid_patterns_are_rejected_before_execution(self):
        self.assertIn("error", call("regex_replace", pattern="(", replacement="x", text="a"))
        self.assertIn("error", call("regex_replace", pattern="(a+)+$", replacement="x",
                                    text="a" * 40))
        self.assertIn("error", call("regex_replace", pattern="a", replacement="x", text="a",
                                    flags="q"))
        self.assertIn("error", call("regex_replace", pattern="a", replacement=r"\9", text="a"))


class GitShowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("git"):
            raise unittest.SkipTest("git is not installed")
        cls.repo = fresh_dir("git-show-repo")
        cls.relative = sandbox_relative(cls.repo)

        def git(*args):
            subprocess.run(["git", "-C", cls.repo, *args], check=True,
                           capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.name", "Upgrade Tester")
        git("config", "user.email", "upgrade@example.invalid")
        with open(os.path.join(cls.repo, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("one\n")
        git("add", ".")
        git("commit", "-qm", "first commit")
        with open(os.path.join(cls.repo, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("one\ntwo\n")
        with open(os.path.join(cls.repo, "b.txt"), "w", encoding="utf-8") as handle:
            handle.write("b\n")
        git("add", ".")
        git("commit", "-qm", "second commit")

    def test_commit_metadata_stat_and_patch(self):
        result = call("git_show", directory=self.relative, ref="HEAD")
        self.assertTrue(result["ok"])
        self.assertEqual(result["subject"], "second commit")
        self.assertEqual(result["author"], "Upgrade Tester")
        self.assertEqual(result["files_changed"], 2)
        self.assertEqual(result["deletions"], 0)
        self.assertIn("diff --git", result["diff"])
        self.assertFalse(result["truncated"])

    def test_older_refs_and_stat_only_mode(self):
        older = call("git_show", directory=self.relative, ref="HEAD~1")
        self.assertEqual(older["subject"], "first commit")
        stat_only = call("git_show", directory=self.relative, stat_only=True)
        self.assertNotIn("diff", stat_only)
        self.assertIn("a.txt", stat_only["stat"])

    def test_unsafe_or_unknown_refs_are_rejected(self):
        for ref in ("-p", "HEAD;rm -rf /", "HEAD && id", "HEAD$(id)"):
            self.assertIn("error", call("git_show", directory=self.relative, ref=ref), ref)
        unknown = call("git_show", directory=self.relative, ref="deadbeef")
        self.assertIn("error", unknown)


class HttpUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen_headers = {}

        class Fixture(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.seen_headers = {k.lower(): v for k, v in self.headers.items()}
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"hello":"world"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.server.seen_headers = {}
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls._saved = mts.ALLOW_PRIVATE_NETWORKS
        mts.ALLOW_PRIVATE_NETWORKS = True

    @classmethod
    def tearDownClass(cls):
        mts.ALLOW_PRIVATE_NETWORKS = cls._saved
        cls.server.shutdown()
        cls.server.server_close()

    def test_default_headers_are_sent(self):
        result = call("http_request", url=f"{self.base}/json")
        self.assertTrue(result["ok"])
        self.assertIn("MCP-Tool-Server", self.server.seen_headers.get("user-agent", ""))
        self.assertEqual(self.server.seen_headers.get("accept"), "*/*")
        self.assertIn("elapsed_ms", result)
        self.assertFalse(result["redirected"])

    def test_redirects_can_be_disabled(self):
        followed = call("http_request", url=f"{self.base}/redirect")
        self.assertEqual(followed["status_code"], 200)
        self.assertTrue(followed["redirected"])

        blocked = call("http_request", url=f"{self.base}/redirect", allow_redirects=False)
        # The redirect is reported rather than followed; 3xx is not a transport error.
        self.assertEqual(blocked["status_code"], 302)
        self.assertFalse(blocked["redirected"])
        self.assertEqual(blocked["headers"].get("location"), "/target")

    def test_custom_headers_override_defaults(self):
        call("http_request", url=f"{self.base}/json", headers=json.dumps({"User-Agent": "custom-agent"}))
        self.assertEqual(self.server.seen_headers.get("user-agent"), "custom-agent")


class SchemaUpgradeTests(unittest.TestCase):
    def test_parameter_descriptions_are_exposed(self):
        schema = mts._build_mcp_input_schema(mts.TOOL_SCHEMAS["web_search"], "web_search")
        self.assertIn("description", schema["properties"]["query"])
        self.assertIn("description", schema["properties"]["max_results"])
        # Tool-specific override wins over the generic hint.
        http_schema = mts._build_mcp_input_schema(mts.TOOL_SCHEMAS["http_request"], "http_request")
        self.assertIn("SSRF", http_schema["properties"]["allow_redirects"]["description"])

    def test_tool_help_examples_are_typed(self):
        help_payload = json.loads(mts.tool_help("web_search"))
        self.assertEqual(help_payload["example"]["args"]["max_results"], 5)
        self.assertIs(help_payload["example"]["args"]["safe_search"], False)
        dangerous = json.loads(mts.tool_help("execute_bash_command"))
        self.assertTrue(dangerous["requires_dangerous_mode"])

    def test_new_tools_are_registered_and_documented(self):
        for name in ("regex_replace", "git_show"):
            self.assertIn(name, mts.TOOL_MAP)
            self.assertIn(name, mts.TOOL_SCHEMAS)
            self.assertIn(name, mts.TOOL_CATEGORIES)
            self.assertTrue(mts.TOOL_MAP[name].__doc__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
