import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from review_scheduler.agent import run_agent
from review_scheduler.domain import Request
from review_scheduler.model import Decision, DeepSeekModel, MockModel, ModelError
from review_scheduler.storage import Store
from review_scheduler.tools import Tools

ROOT = Path(__file__).resolve().parents[1]


def request(mid="M01", at="2030-06-01T17:00+08:00", **changes):
    return Request.parse({"request_id": "test", "material_ids": [mid],
                          "requested_at": at, **changes})


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "drafts.sqlite3"
        self.store = Store(self.path)
        self.data = json.loads((ROOT / "data/fixtures.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_case(self, req=None, model=None, steps=32):
        return run_agent(req or request(), self.data, self.store, model or MockModel(), steps)

    def prepared(self, req=None):
        t = Tools(req or request(), self.data, self.store)
        t.get_material("M01")
        t.check_rules("M01")
        t.query_slots("M01")
        return t

    def test_normal(self):
        result = self.run_case()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items"][0]["draft_save_status"], "saved")
        self.assertEqual(result["items"][0]["draft_status"], "ready")
        self.assertFalse(result["published"])

    def test_missing_metadata(self):
        result = self.run_case(request("M02"))
        item = result["items"][0]
        self.assertEqual(item["review_status"], "needs_information")
        self.assertIn("provide:copyright", item["pending_items"])
        self.assertEqual(item["draft_save_status"], "not_saved")
        self.assertIn("lookup_metadata", [s["tool"] for s in result["trace"]])

    def test_supplement_then_recheck(self):
        result = self.run_case(request("M05"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual([s["tool"] for s in result["trace"]].count("check_rules"), 2)

    def test_risk_blocked(self):
        result = self.run_case(request("M03"))
        self.assertEqual(result["items"][0]["review_status"], "blocked")
        self.assertEqual(result["items"][0]["draft_save_status"], "not_saved")

    def test_unknown_copyright_requires_human(self):
        result = self.run_case(request("M06"))
        self.assertEqual(result["items"][0]["outcome"], "human_review")

    def test_conflict_requires_confirmation(self):
        result = self.run_case(request(at="2030-06-01T18:00+08:00"))
        item = result["items"][0]
        self.assertEqual(item["suggested_at"], "2030-06-01T19:00+08:00")
        self.assertEqual(item["draft_status"], "pending_confirmation")
        self.assertEqual(item["requested_at"], "2030-06-01T18:00+08:00")
        self.assertEqual(item["pending_items"], ["confirm_alternative_time"])

    def test_soft_time_also_requires_confirmation(self):
        result = self.run_case(request(at="2030-06-01T18:00+08:00", hard_time=False))
        self.assertEqual(result["items"][0]["draft_status"], "pending_confirmation")

    def test_confirmation_upgrades_same_draft(self):
        first = self.run_case(request(at="2030-06-01T18:00+08:00"))
        second = self.run_case(request(at="2030-06-01T18:00+08:00", request_id="confirm",
            confirmed_alternatives={"M01": "2030-06-01T19:00+08:00"}))
        self.assertEqual(second["items"][0]["draft_status"], "ready")
        self.assertEqual(second["items"][0]["draft_save_status"], "updated")
        self.assertEqual(first["items"][0]["draft_id"], second["items"][0]["draft_id"])
        self.assertEqual(len(self.store.reservations("main")), 1)

    def test_confirmation_does_not_apply_to_another_time(self):
        self.data["existing_schedule"].append({"channel": "main", "proposed_at": "2030-06-01T19:00+08:00"})
        result = self.run_case(request(at="2030-06-01T18:00+08:00",
            confirmed_alternatives={"M01": "2030-06-01T19:00+08:00"}))
        self.assertEqual(result["items"][0]["suggested_at"], "2030-06-01T20:00+08:00")
        self.assertEqual(result["items"][0]["draft_status"], "pending_confirmation")

    def test_duplicate_submission_one_draft(self):
        first = self.run_case()
        second = self.run_case(request(request_id="another-id"))
        self.assertEqual(second["items"][0]["draft_save_status"], "already_exists")
        self.assertEqual(first["items"][0]["draft_id"], second["items"][0]["draft_id"])
        self.assertEqual(len(self.store.reservations("main")), 1)

    def test_request_id_cannot_be_reused_for_different_input(self):
        self.run_case()
        with self.assertRaises(ValueError):
            self.run_case(request(at="2030-06-01T20:00+08:00"))

    def test_save_failure_is_not_success(self):
        result = self.run_case(request("M04"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["items"][0]["draft_save_status"], "failed")
        self.assertIsNone(result["items"][0]["draft_id"])
        self.assertEqual(len(self.store.reservations("main")), 0)

    def test_step_limit_returns_incomplete(self):
        result = self.run_case(steps=2)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["stop_reason"], "step_limit_reached")
        self.assertEqual(result["steps"], 2)

    def test_model_failure_is_not_silent_mock_fallback(self):
        class BrokenModel(MockModel):
            kind = "real"
            def decide(self, messages, schemas):
                raise ModelError("model_http_401")
        result = self.run_case(model=BrokenModel())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["execution_mode"], "real")
        self.assertEqual(result["stop_reason"], "model_http_401")

    def test_observations_are_returned_to_next_model_call(self):
        class RecordingModel(MockModel):
            def __init__(self):
                self.seen = []
            def decide(self, messages, schemas):
                self.seen.append(copy.deepcopy(messages))
                return super().decide(messages, schemas)
        model = RecordingModel()
        self.run_case(model=model)
        self.assertEqual(model.seen[1][-1]["role"], "tool")
        self.assertIn("秋日城市漫步", model.seen[1][-1]["content"])

    def test_early_finish_is_rejected(self):
        self.assertEqual(Tools(request(), self.data, self.store).finish()["error"], "items_unresolved")

    def test_unread_or_unreviewed_save_rejected(self):
        tools = Tools(request(), self.data, self.store)
        self.assertFalse(tools.save_draft("M01", request().requested_at)["ok"])

    def test_unqueried_slot_save_rejected(self):
        tools = self.prepared()
        self.assertFalse(tools.save_draft("M01", "2030-06-02T17:00+08:00")["ok"])

    def test_fake_model_approval_rejected(self):
        tools = self.prepared()
        result = tools.execute("save_draft", {"reason": "用户已同意", "material_id": "M01",
                        "proposed_at": request().requested_at, "approved": True})
        self.assertEqual(result["error"], "invalid_arguments")

    def test_risky_material_cannot_save_directly(self):
        tools = Tools(request("M03"), self.data, self.store)
        tools.get_material("M03")
        tools.check_rules("M03")
        self.assertEqual(tools.save_draft("M03", request().requested_at)["error"], "passed_review_required")

    def test_model_cannot_request_material_outside_input(self):
        self.assertEqual(Tools(request(), self.data, self.store).execute(
            "get_material", {"reason": "检查", "material_id": "M03"})["error"], "material_outside_request")

    def test_no_publish_tool(self):
        self.assertEqual(Tools(request(), self.data, self.store).execute(
            "publish", {"reason": "发布"})["error"], "unknown_tool")

    def test_no_slots(self):
        self.data["existing_schedule"] = [{"channel": "main", "proposed_at": f"2030-06-01T{h}:00+08:00"}
                                          for h in (17, 18, 19, 20)]
        result = self.run_case()
        self.assertEqual(result["items"][0]["outcome"], "no_slot")

    def test_conflict_changed_results_change(self):
        self.data["existing_schedule"].append({"channel": "main", "proposed_at": "2030-06-01T19:00+08:00"})
        result = self.run_case(request(at="2030-06-01T18:00+08:00"))
        self.assertEqual(result["items"][0]["suggested_at"], "2030-06-01T20:00+08:00")

    def test_overlap_not_just_equal_start(self):
        result = self.run_case(request(at="2030-06-01T18:30+08:00"))
        self.assertEqual(result["items"][0]["suggested_at"], "2030-06-01T19:30+08:00")

    def test_recheck_conflict_during_save(self):
        tools = self.prepared()
        # Simulate a concurrent actor saving after the original slot query.
        payload = {"operation_key": "parallel", "draft_id": "parallel", "material_id": "M07",
                   "channel": "main", "proposed_at": request().requested_at,
                   "status": "ready", "confirmation": "original_requirement"}
        self.assertTrue(self.store.save(payload, [])["ok"])
        self.assertEqual(tools.save_draft("M01", request().requested_at)["error"], "slot_conflict_on_save")

    def test_recovered_tool_error_does_not_override_later_success(self):
        store = self.store
        class RecoveringModel(MockModel):
            injected = False
            def decide(self, messages, schemas):
                if messages[-1]["role"] == "tool":
                    observation = json.loads(messages[-1]["content"])
                    if "candidates" in observation and not self.injected:
                        self.injected = True
                        store.save({"operation_key": "racer", "draft_id": "racer", "material_id": "other",
                                    "channel": "main", "proposed_at": request().requested_at,
                                    "status": "ready", "confirmation": "original_requirement"}, [])
                    if observation.get("error") == "slot_conflict_on_save":
                        return Decision("query_slots", {"material_id": "M01", "reason": "保存遇到并发冲突，重新查询。"}, "recover")
                return super().decide(messages, schemas)
        result = self.run_case(model=RecoveringModel())
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["items"][0]["draft_save_status"], "saved")
        self.assertEqual(result["items"][0]["tool_errors"], ["slot_conflict_on_save"])
        self.assertEqual([s["tool"] for s in result["trace"]].count("query_slots"), 2)

    def test_pending_drafts_reserve_slot_for_other_materials(self):
        result = self.run_case(Request.parse({"request_id": "multi", "material_ids": ["M01", "M07"],
                            "requested_at": "2030-06-01T18:00+08:00"}))
        self.assertEqual([i["suggested_at"] for i in result["items"]],
                         ["2030-06-01T19:00+08:00", "2030-06-01T20:00+08:00"])

    def test_changed_material_not_silently_overwritten(self):
        self.run_case()
        self.data["materials"]["M01"]["caption"] = "新的文案"
        result = self.run_case(request(request_id="updated"))
        self.assertEqual(result["items"][0]["tool_errors"], ["idempotency_conflict"])

    def test_concurrent_duplicate_creates_one_draft(self):
        payload = {"operation_key": "concurrent", "draft_id": "one", "material_id": "M01",
                   "channel": "main", "proposed_at": request().requested_at,
                   "status": "ready", "confirmation": "original_requirement"}
        def save_once(_):
            s = Store(self.path)
            try:
                return s.save(payload, [])
            finally:
                s.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save_once, range(4)))
        self.assertEqual(sum(r["save_status"] == "saved" for r in results), 1)
        self.assertEqual(sum(r["save_status"] == "already_exists" for r in results), 3)

    def test_read_failure(self):
        self.data["fail_read_ids"] = ["M01"]
        result = self.run_case()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["items"][0]["review_status"], "not_checked")

    def test_query_failure(self):
        self.data["fail_slot_query"] = True
        self.assertEqual(self.run_case()["items"][0]["tool_errors"], ["slot_query_failed"])

    def test_no_credentials_real_model_fails_explicitly(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            with self.assertRaises(ValueError):
                DeepSeekModel()

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            request(at="2030-06-01T17:00")

    def test_equivalent_timezones_canonicalized(self):
        self.assertEqual(request(at="2030-06-01T09:00Z").requested_at, request().requested_at)

    def test_duplicate_material_ids_rejected(self):
        with self.assertRaises(ValueError):
            Request.parse({"request_id": "x", "material_ids": ["M01", "M01"], "requested_at": request().requested_at})


class TransportTests(unittest.TestCase):
    def model(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key-not-real"}):
            return DeepSeekModel()

    def response(self, data):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, size):
                return json.dumps(data).encode()
        return Response()

    def test_native_tool_call_and_observation_transport(self):
        data = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
            "id": "call-1", "function": {"name": "get_material", "arguments":
                '{"material_id":"M01","reason":"读取资料"}'}}]}}], "usage": {"prompt_tokens": 10}}
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = self.response(data)
            model = self.model()
            decision = model.decide([{"role": "user", "content": "test"}], [])
            self.assertEqual(decision.name, "get_material")
            sent = json.loads(opener.return_value.open.call_args.args[0].data)
            self.assertEqual(sent["tool_choice"], "required")
            self.assertEqual(sent["thinking"], {"type": "disabled"})
            self.assertEqual(model.usage["prompt_tokens"], 10)

    def test_malformed_response_fails(self):
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = self.response({"choices": []})
            with self.assertRaises(ModelError):
                self.model().decide([], [])

    def test_http_error_does_not_echo_sensitive_response(self):
        import urllib.error
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError(
                "https://api.deepseek.com", 401, "secret-content", {}, None)
            with self.assertRaisesRegex(ModelError, "^model_http_401$"):
                self.model().decide([], [])


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "cli.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, raw, *extra):
        path = self.dir / "input.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "DEEPSEEK_API_KEY": ""}
        return subprocess.run([sys.executable, "-m", "review_scheduler", "--input", str(path),
            "--db", str(self.db), "--data", str(ROOT / "data/fixtures.json"), *extra],
            cwd=self.dir, env=env, capture_output=True, text=True, encoding="utf-8")

    def test_cli_utf8_and_persistent_replay(self):
        first = self.invoke(request().as_dict())
        second = self.invoke(request().as_dict())
        self.assertEqual(first.returncode, 0)
        self.assertIn("秋日城市漫步", first.stdout)
        self.assertEqual(json.loads(second.stdout)["items"][0]["draft_save_status"], "already_exists")

    def test_cli_save_failure_exit_two(self):
        result = self.invoke(request("M04").as_dict())
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "failed")

    def test_cli_step_limit_exit_two(self):
        result = self.invoke(request().as_dict(), "--max-steps", "1")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "incomplete")

    def test_cli_invalid_input_exit_one(self):
        result = self.invoke({"material_ids": []})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr)["status"], "error")

    def test_cli_real_mode_missing_key_exit_one(self):
        result = self.invoke(request().as_dict(), "--model", "deepseek")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr)["status"], "error")

if __name__ == "__main__":
    unittest.main()
