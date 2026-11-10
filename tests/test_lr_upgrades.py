#!/usr/bin/env python3
"""Regression tests for the lr.py v4.0 upgrade pass.

Everything here is offline: the webhook pipeline is exercised through Flask's
test client with `query_ollama` stubbed, and the pure helpers are called
directly. Each test pins a behaviour that was broken, missing or inconsistent
in v3.7.3, plus the wire-compatibility guarantees the AutoReply app relies on.

    python3 -m unittest tests.test_lr_upgrades -v
"""
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

# lr.py reads $HOME for its DB / log paths at import time — keep it in a sandbox.
_SANDBOX = os.path.join(tempfile.gettempdir(), "lr_upgrade_tests")
shutil.rmtree(_SANDBOX, ignore_errors=True)
os.makedirs(_SANDBOX, exist_ok=True)
os.environ["HOME"] = _SANDBOX

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import lr  # noqa: E402
    lr.logger.setLevel(logging.CRITICAL)  # keep test output readable
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - dependency guard
    lr = None
    IMPORT_ERROR = exc


@unittest.skipIf(lr is None, f"lr.py dependencies unavailable: {IMPORT_ERROR}")
class WebhookSchemaTests(unittest.TestCase):
    """The AutoReply wire contract must never change."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=_SANDBOX)
        lr.CONFIG["database"]["path"] = os.path.join(self.tmp, "conv.db")
        lr.CONFIG["notifications"]["enabled"] = False
        lr.CONFIG["security"].update({
            "webhook_token": None,
            "require_signature": False,
            "rate_limit_per_minute": 1000,
            "ip_rate_limit_per_minute": 100000,
            "min_reply_interval_seconds": 0,
            "allowed_app_ids": [],
            "ignore_sender_patterns": [],
            "ignore_message_patterns": [],
            "max_message_age_seconds": 300,
        })
        lr.CONFIG["security"]["quiet_hours"]["enabled"] = False
        lr.CONFIG["ollama"].update({"model": "test-model", "fallback_models": []})
        lr.close_db_connections()
        lr.init_db()
        lr.dedup_engine.cache.clear()
        lr.dedup_engine.in_flight.clear()
        lr.dedup_engine.in_flight_results.clear()
        lr.self_message_guard.reset()
        lr.rate_limiter.reset()
        self.client = lr.app.test_client()
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()

    def tearDown(self):
        self.stdout.__exit__(None, None, None)
        lr.close_db_connections()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hook(self, payload, **kwargs):
        return self.client.post("/pyrmethus_autoreply_webhook", json=payload, **kwargs)

    def test_strict_response_schema(self):
        with mock.patch.object(lr, "query_ollama", return_value="yeah prob"):
            response = self.hook({"senderName": "Maya", "senderMessage": "coming saturday?"})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(set(body), {"data"})
        self.assertEqual(set(body["data"][0]), {"message"})
        self.assertEqual(body["data"][0]["message"], "yeah prob")

    def test_nested_envelope_and_tk_studio_aliases(self):
        with mock.patch.object(lr, "query_ollama",
                               return_value="got it") as llm:
            response = self.hook({"data": [{
                "senderName": "Maya",
                "senderMessage": "hey",
                "receiveMessageAppId": "com.whatsapp",
                "isMessageFromGroup": "false",
            }]})
        self.assertEqual(response.get_json()["data"][0]["message"], "got it")
        system = llm.call_args[0][0][0]["content"]
        self.assertIn("Maya", system)
        self.assertIn("WhatsApp", system)

    def test_nested_query_object(self):
        with mock.patch.object(lr, "query_ollama", return_value="nested ok"):
            response = self.hook({"query": {"sender": "Q", "message": "hi from query"}})
        self.assertEqual(response.get_json()["data"][0]["message"], "nested ok")

    def test_empty_payload_returns_empty_data(self):
        self.assertEqual(self.hook({}).get_json(), {"data": []})
        self.assertEqual(self.hook({"senderName": "X"}).get_json(), {"data": []})

    def test_bounds_rejected_with_400(self):
        response = self.hook({"senderName": "X", "senderMessage": "z" * 5000})
        self.assertEqual(response.status_code, 400)

    def test_stale_message_dropped(self):
        old = int((time.time() - 3600) * 1000)
        response = self.hook({"senderName": "Old", "senderMessage": "hi", "messageDateTime": old})
        self.assertEqual(response.get_json(), {"data": []})

    def test_future_message_dropped(self):
        ahead = int((time.time() + 7200) * 1000)
        response = self.hook({"senderName": "Future", "senderMessage": "hi", "messageDateTime": ahead})
        self.assertEqual(response.get_json(), {"data": []})

    def test_self_sender_suppressed(self):
        self.assertEqual(self.hook({"senderName": "Pyrmethus", "senderMessage": "loop"}).get_json(),
                         {"data": []})

    def test_echo_of_own_reply_suppressed(self):
        lr.self_message_guard.remember_outgoing("Maya", "yeah prob")
        self.assertEqual(self.hook({"senderName": "Maya", "senderMessage": "yeah prob"}).get_json(),
                         {"data": []})

    def test_blocklist_suppressed(self):
        self.assertEqual(self.hook({"senderName": "WhatsApp", "senderMessage": "code"}).get_json(),
                         {"data": []})

    def test_duplicate_delivery_is_cached(self):
        with mock.patch.object(lr, "query_ollama", return_value="only once") as llm:
            first = self.hook({"senderName": "Maya", "senderMessage": "same text"})
            second = self.hook({"senderName": "Maya", "senderMessage": "same text"})
        self.assertEqual(first.get_json(), second.get_json())
        self.assertEqual(llm.call_count, 1)

    def test_same_text_two_groups_not_shared(self):
        with mock.patch.object(lr, "query_ollama", return_value="x") as llm:
            self.hook({"senderName": "M", "senderMessage": "hi", "isMessageFromGroup": True,
                       "groupName": "G1"})
            self.hook({"senderName": "M", "senderMessage": "hi", "isMessageFromGroup": True,
                       "groupName": "G2"})
        self.assertEqual(llm.call_count, 2)

    def test_group_context_reaches_prompt(self):
        with mock.patch.object(lr, "query_ollama", return_value="ok") as llm:
            self.hook({"senderName": "M", "senderMessage": "hi", "isMessageFromGroup": True,
                       "groupName": "Family", "receiveMessageAppId": "com.whatsapp"})
        system = llm.call_args[0][0][0]["content"]
        self.assertIn("group chat", system)
        self.assertIn("Family", system)

    def test_llm_failure_returns_fallback_not_error(self):
        with mock.patch.object(lr, "query_ollama", side_effect=RuntimeError("down")):
            response = self.hook({"senderName": "Maya", "senderMessage": "hi"})
        self.assertEqual(response.status_code, 200)
        message = response.get_json()["data"][0]["message"]
        self.assertIn(message, lr.CONFIG["persona"]["fallback_replies"])

    def test_history_is_used_for_followups(self):
        with mock.patch.object(lr, "query_ollama", return_value="first reply") as llm:
            self.hook({"senderName": "Maya", "senderMessage": "first question"})
            self.hook({"senderName": "Maya", "senderMessage": "second question"})
        messages = llm.call_args[0][0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["content"], "second question")
        self.assertIn("first question", [m["content"] for m in messages])

    def test_conversation_is_persisted(self):
        with mock.patch.object(lr, "query_ollama", return_value="stored"):
            self.hook({"senderName": "Maya", "senderMessage": "persist me",
                       "receiveMessageAppId": "com.whatsapp"})
        rows = lr.get_conversations(limit=5)
        self.assertEqual(rows[0]["sender"], "Maya")
        self.assertEqual(rows[0]["response"], "stored")
        self.assertEqual(rows[0]["app_name"], "WhatsApp")

    def test_alias_routes_share_pipeline(self):
        with mock.patch.object(lr, "query_ollama", return_value="via alias"):
            response = self.client.post("/webhook", json={"senderName": "A", "senderMessage": "hi"})
        self.assertEqual(response.get_json()["data"][0]["message"], "via alias")

    def test_root_post_also_accepts_webhook(self):
        with mock.patch.object(lr, "query_ollama", return_value="root ok"):
            response = self.client.post("/", json={"senderName": "A", "senderMessage": "hi2"})
        self.assertEqual(response.get_json()["data"][0]["message"], "root ok")

    def test_ignore_message_pattern(self):
        lr.CONFIG["security"]["ignore_message_patterns"] = [r"(?i)\b(otp|verification)\b"]
        self.assertEqual(self.hook({"senderName": "Bank", "senderMessage": "your OTP is 1234"}).get_json(),
                         {"data": []})

    def test_app_allowlist(self):
        lr.CONFIG["security"]["allowed_app_ids"] = ["com.whatsapp"]
        response = self.hook({"senderName": "T", "senderMessage": "hi",
                              "receiveMessageAppId": "org.telegram.messenger"})
        self.assertEqual(response.get_json(), {"data": []})

    def test_quiet_hours_drop_and_fallback(self):
        now = datetime.now(timezone.utc)
        lr.CONFIG["security"]["quiet_hours"] = {
            "enabled": True,
            "start": (now - timedelta(minutes=30)).strftime("%H:%M"),
            "end": (now + timedelta(minutes=30)).strftime("%H:%M"),
            "action": "drop", "utc_offset_hours": None,
        }
        with mock.patch.object(lr, "query_ollama", return_value="should not run") as llm:
            self.assertEqual(self.hook({"senderName": "Q", "senderMessage": "hi"}).get_json(), {"data": []})
            self.assertEqual(llm.call_count, 0)

        lr.CONFIG["security"]["quiet_hours"]["action"] = "fallback"
        body = self.hook({"senderName": "Q", "senderMessage": "hi2"}).get_json()
        self.assertIn(body["data"][0]["message"], lr.CONFIG["persona"]["fallback_replies"])

    def test_reply_cooldown(self):
        lr.CONFIG["security"]["min_reply_interval_seconds"] = 30
        with mock.patch.object(lr, "query_ollama", return_value="ok"):
            first = self.hook({"senderName": "Fast", "senderMessage": "one"})
            second = self.hook({"senderName": "Fast", "senderMessage": "two"})
        self.assertTrue(first.get_json()["data"])
        self.assertEqual(second.get_json(), {"data": []})


@unittest.skipIf(lr is None, f"lr.py dependencies unavailable: {IMPORT_ERROR}")
class AuthTests(unittest.TestCase):
    """Token and HMAC-signature auth (signature support was dead config in v3.7.3)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=_SANDBOX)
        lr.CONFIG["database"]["path"] = os.path.join(self.tmp, "conv.db")
        lr.CONFIG["notifications"]["enabled"] = False
        lr.CONFIG["security"].update({"webhook_token": None, "require_signature": False,
                                      "rate_limit_per_minute": 1000,
                                      "ip_rate_limit_per_minute": 100000})
        lr.close_db_connections()
        lr.init_db()
        lr.rate_limiter.reset()
        lr._secret_cache.clear()
        self.client = lr.app.test_client()
        self._out = contextlib.redirect_stdout(io.StringIO())
        self._out.__enter__()

    def tearDown(self):
        self._out.__exit__(None, None, None)
        lr.close_db_connections()
        lr.CONFIG["security"].update({"webhook_token": None, "require_signature": False})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_token_required(self):
        lr.CONFIG["security"]["webhook_token"] = "sekret"
        with mock.patch.object(lr, "query_ollama", return_value="ok"):
            self.assertEqual(self.client.post("/pyrmethus_autoreply_webhook",
                                              json={"senderName": "A", "senderMessage": "hi"}).status_code, 401)
            self.assertEqual(self.client.post("/pyrmethus_autoreply_webhook",
                                              json={"senderName": "A", "senderMessage": "hi"},
                                              headers={"Authorization": "Bearer wrong"}).status_code, 401)
            ok = self.client.post("/pyrmethus_autoreply_webhook",
                                  json={"senderName": "A", "senderMessage": "hi"},
                                  headers={"Authorization": "Bearer sekret"})
        self.assertEqual(ok.status_code, 200)

    def test_auth_runs_before_rate_limit(self):
        lr.CONFIG["security"]["webhook_token"] = "sekret"
        lr.CONFIG["security"]["rate_limit_per_minute"] = 1
        baseline = lr.metrics.counter("rate_limited")
        for _ in range(5):
            self.client.post("/pyrmethus_autoreply_webhook", json={"senderName": "A", "senderMessage": "x"})
        self.assertEqual(lr.metrics.counter("rate_limited"), baseline)

    def test_signature_verification(self):
        secret_path = os.path.join(self.tmp, "secret")
        with open(secret_path, "w") as handle:
            handle.write("topsecret\n")
        lr.CONFIG["security"].update({"require_signature": True, "secret_file": secret_path,
                                      "signature_tolerance_seconds": 300})
        raw = json.dumps({"senderName": "S", "senderMessage": "signed"})
        headers = {"Content-Type": "application/json"}

        self.assertEqual(self.client.post("/webhook", data=raw, headers=headers).status_code, 401)

        bad = hmac.new(b"nope", raw.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(self.client.post("/webhook", data=raw,
                                          headers={**headers, "X-Pyrmethus-Signature": bad}).status_code, 401)

        good = hmac.new(b"topsecret", raw.encode(), hashlib.sha256).hexdigest()
        with mock.patch.object(lr, "query_ollama", return_value="signed ok"):
            ok = self.client.post("/webhook", data=raw,
                                  headers={**headers, "X-Pyrmethus-Signature": f"sha256={good}"})
        self.assertEqual(ok.status_code, 200)

    def test_signature_replay_window(self):
        secret_path = os.path.join(self.tmp, "secret2")
        with open(secret_path, "w") as handle:
            handle.write("topsecret")
        lr.CONFIG["security"].update({"require_signature": True, "secret_file": secret_path,
                                      "signature_tolerance_seconds": 60})
        raw = json.dumps({"senderName": "S", "senderMessage": "old"})
        good = hmac.new(b"topsecret", raw.encode(), hashlib.sha256).hexdigest()
        response = self.client.post("/webhook", data=raw, headers={
            "Content-Type": "application/json",
            "X-Pyrmethus-Signature": good,
            "X-Signature-Timestamp": str(int(time.time()) - 3600),
        })
        self.assertEqual(response.status_code, 401)


@unittest.skipIf(lr is None, f"lr.py dependencies unavailable: {IMPORT_ERROR}")
class HelperTests(unittest.TestCase):
    """Pure-function behaviour."""

    def test_clean_strips_reasoning_and_labels(self):
        cases = {
            "<think>long thought</think>hey there": "hey there",
            "[think]x[/think]yo": "yo",
            "Assistant: yo": "yo",
            'Jeremiah: "hey"': "hey",
            "<|im_end|>hi": "hi",
            "```\nhi\n```": "hi",
            "thought</think>reply": "reply",
            "": "",
        }
        for raw, expected in cases.items():
            self.assertEqual(lr.clean_llm_response(raw), expected, raw)

    def test_clean_keeps_apostrophes(self):
        self.assertEqual(lr.clean_llm_response("i'm good, don't worry"), "i'm good, don't worry")

    def test_clean_clamps_on_sentence_boundary(self):
        result = lr.clean_llm_response("one. two. three. four.", max_chars=12)
        self.assertLessEqual(len(result), 12)
        self.assertTrue(result.endswith("."))

    def test_bool_field(self):
        self.assertIs(lr.parse_bool_field("false"), False)
        self.assertIs(lr.parse_bool_field("true"), True)
        self.assertIs(lr.parse_bool_field("0"), False)
        self.assertIs(lr.parse_bool_field(1), True)
        self.assertIs(lr.parse_bool_field(None), False)

    def test_message_age(self):
        now = time.time()
        self.assertLess(abs(lr.parse_message_age(int(now * 1000))), 5)
        self.assertLess(abs(lr.parse_message_age(int(now))), 5)
        self.assertIsNotNone(lr.parse_message_age("1762764000000"))
        self.assertIsNone(lr.parse_message_age("not a date"))
        self.assertIsNone(lr.parse_message_age(None))
        self.assertLess(lr.parse_message_age(int((now + 60) * 1000)), 0)

    def test_quiet_hours_wraparound(self):
        original = dict(lr.CONFIG["security"]["quiet_hours"])
        try:
            lr.CONFIG["security"]["quiet_hours"].update({"enabled": True, "start": "23:00",
                                                         "end": "07:00", "utc_offset_hours": None})
            at = lambda h, m=0: datetime(2026, 11, 10, h, m, tzinfo=timezone.utc)
            self.assertTrue(lr.in_quiet_hours(at(23, 30)))
            self.assertTrue(lr.in_quiet_hours(at(3)))
            self.assertFalse(lr.in_quiet_hours(at(12)))
            self.assertFalse(lr.in_quiet_hours(at(7)))
            lr.CONFIG["security"]["quiet_hours"]["enabled"] = False
            self.assertFalse(lr.in_quiet_hours(at(23, 30)))
        finally:
            lr.CONFIG["security"]["quiet_hours"] = original

    def test_build_chat_messages_trims_history(self):
        history = [{"message": "a" * 2000, "response": "b" * 2000} for _ in range(6)]
        messages = lr.build_chat_messages("M", "current", history)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["content"], "current")
        self.assertEqual(messages[1]["role"], "user")
        total = sum(len(m["content"]) for m in messages)
        self.assertLessEqual(total, lr.CONFIG["context"]["max_context_chars"] + 200)

    def test_dedup_single_flight(self):
        engine = lr.DeduplicationEngine(ttl_seconds=5)
        key = engine.make_key("A", "b", "")
        leader, _ = engine.acquire(key)
        self.assertTrue(leader)
        engine.release(key, {"data": [{"message": "done"}]})
        follower, payload = engine.acquire(key)
        self.assertFalse(follower)
        self.assertEqual(payload["data"][0]["message"], "done")

    def test_dedup_dead_leader_does_not_wedge(self):
        engine = lr.DeduplicationEngine(ttl_seconds=5)
        engine.follower_wait = 0.05
        engine.stale_after = 0.05
        key = engine.make_key("A", "b", "")
        self.assertTrue(engine.acquire(key)[0])
        time.sleep(0.1)
        self.assertTrue(engine.acquire(key)[0])

    def test_dedup_key_scoping(self):
        engine = lr.DeduplicationEngine()
        self.assertNotEqual(engine.make_key("A", "hi", "G1"), engine.make_key("A", "hi", "G2"))
        self.assertNotEqual(engine.make_key("A", "hi", "", "m1"), engine.make_key("A", "hi", "", "m2"))
        self.assertEqual(engine.make_key("A", "hi", ""), engine.make_key("a", "hi", ""))

    def test_rate_limiter_window(self):
        limiter = lr.RateLimiter()
        self.assertEqual([limiter.allow("k", 2, 60)[0] for _ in range(3)], [True, True, False])
        allowed, retry = limiter.allow("k", 2, 60)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)
        self.assertTrue(limiter.allow("other", 2, 60)[0])

    def test_self_guard_budget_and_repeats(self):
        guard = lr.SelfMessageGuard()
        self.assertTrue(guard.is_self_sender("Pyrmethus"))
        self.assertFalse(guard.is_self_sender("Maya"))
        guard.remember_outgoing("Maya", "sure thing")
        self.assertTrue(guard.inspect("Maya", "sure thing")[0])

        limit = lr.SelfMessageGuard()
        for index in range(limit.max_replies):
            self.assertTrue(limit.allow_reply("spam", f"msg {index}"))
        self.assertFalse(limit.allow_reply("spam", "msg final"))

        repeats = lr.SelfMessageGuard()
        repeats.allow_reply("x", "same")
        repeats.allow_reply("x", "same")
        self.assertFalse(repeats.allow_reply("x", "same"))

    def test_metrics_snapshot_shape(self):
        engine = lr.MetricsEngine()
        engine.inc("x", 2)
        engine.observe_latency(100)
        engine.observe_latency(300)
        engine.add_tokens(10, 5)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["counters"]["x"], 2)
        self.assertEqual(snapshot["latency_ms"]["avg"], 200.0)
        self.assertLessEqual(snapshot["latency_ms"]["p50"], snapshot["latency_ms"]["p95"])
        self.assertEqual(snapshot["tokens"]["total"], 15)
        self.assertIn("pyrmethus_uptime_seconds", engine.prometheus())

    def test_terminal_width_helpers(self):
        self.assertEqual(lr._display_width("📱"), 2)
        self.assertEqual(lr._display_width("\x1b[31mabc\x1b[0m"), 3)
        self.assertEqual(lr._display_width(lr._pad("ab", 6)), 6)

    def test_render_message_card_borders_align(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            lr.render_message_card("Maya", "hey " * 40, "sure " * 40, "com.whatsapp",
                                   True, "Family", 342.0, "test-model", False)
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        widths = {lr._display_width(line) for line in lines}
        self.assertEqual(len(widths), 1, widths)

    def test_coerce_rejects_bad_values(self):
        section = {"timeout": "not-a-number"}
        with contextlib.redirect_stdout(io.StringIO()):
            value = lr._coerce(section, "timeout", int, 25, minimum=1)
        self.assertEqual(value, 25)
        self.assertEqual(lr._coerce({"x": -5}, "x", int, 3, minimum=1), 1)

    def test_pid_safety(self):
        self.assertFalse(lr._pid_is_this_script(os.getpid()))
        self.assertFalse(lr._pid_is_this_script(0))


@unittest.skipIf(lr is None, f"lr.py dependencies unavailable: {IMPORT_ERROR}")
class EndpointTests(unittest.TestCase):
    """Dashboard / observability endpoints."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=_SANDBOX)
        lr.CONFIG["database"]["path"] = os.path.join(self.tmp, "conv.db")
        lr.CONFIG["notifications"]["enabled"] = False
        lr.close_db_connections()
        lr.init_db()
        lr.log_conversation("Maya", "hey", "yo", "com.whatsapp", False, "", "rid", 120.0, "test-model")
        self.client = lr.app.test_client()

    def tearDown(self):
        lr.close_db_connections()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dashboard_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Pyrmethus", body)
        self.assertNotIn("{{", body)
        self.assertIn("Content-Security-Policy", response.headers)

    def test_dashboard_payload(self):
        payload = self.client.get("/api/dashboard").get_json()
        for key in ("stats", "metrics", "conversations", "hourly", "apps", "health", "webhook_url"):
            self.assertIn(key, payload)
        self.assertEqual(len(payload["hourly"]), 24)
        self.assertEqual(payload["conversations"][0]["app_name"], "WhatsApp")
        self.assertTrue(payload["conversations"][0]["display_time"])

    def test_stats_superset(self):
        stats = self.client.get("/stats").get_json()
        for key in ("total_conversations", "unique_senders", "recent_activity_last_hour",
                    "avg_latency_ms_24h", "top_senders_24h", "version"):
            self.assertIn(key, stats)
        self.assertEqual(stats["version"], lr.VERSION)

    def test_metrics_and_prometheus(self):
        metrics = self.client.get("/metrics").get_json()
        self.assertIn("counters", metrics)
        self.assertIn("p99", metrics["latency_ms"])
        text = self.client.get("/metrics/prometheus").get_data(as_text=True)
        self.assertIn("pyrmethus_uptime_seconds", text)

    def test_health_and_live(self):
        self.assertEqual(self.client.get("/live").status_code, 200)
        health = self.client.get("/health").get_json()
        self.assertIn("checks", health)
        self.assertIn("version", health)
        self.assertIn(health["status"], ("healthy", "degraded", "unhealthy"))

    def test_config_endpoint_hides_secrets(self):
        lr.CONFIG["security"]["webhook_token"] = "super-secret-value"
        try:
            body = self.client.get("/api/config").get_data(as_text=True)
        finally:
            lr.CONFIG["security"]["webhook_token"] = None
        self.assertNotIn("super-secret-value", body)

    def test_conversation_filters(self):
        lr.log_conversation("Dev", "build broke", "oof", "org.telegram.messenger")
        result = self.client.get("/api/conversations?q=build").get_json()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["data"][0]["sender"], "Dev")

    def test_request_id_echo(self):
        response = self.client.get("/live", headers={"X-Request-ID": "trace-me"})
        self.assertEqual(response.headers["X-Request-ID"], "trace-me")

    def test_oversized_payload_413(self):
        response = self.client.post("/pyrmethus_autoreply_webhook",
                                    data=json.dumps({"senderName": "x", "senderMessage": "z" * 70000}),
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    def test_favicon(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 200)
        self.assertIn("svg", response.headers["Content-Type"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
