"""Native tool calling; MockModel is explicitly a deterministic test double."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


class ModelError(Exception):
    pass


@dataclass
class Decision:
    name: str
    arguments: dict
    call_id: str = ""

    def message(self):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": self.call_id, "type": "function", "function": {
                "name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)}}]}


def load_env(path=".env"):
    """Minimal dotenv loader. Existing process environment takes priority."""
    if not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("invalid .env line; use KEY=value")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key.isidentifier():
            raise ValueError("invalid .env key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


class DeepSeekModel:
    kind = "real"

    def __init__(self):
        self.key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not self.key:
            raise ValueError("DEEPSEEK_API_KEY is required for --model deepseek; use --model mock offline")
        self.base = os.getenv("MODEL_BASE_URL", "https://api.deepseek.com").rstrip("/")
        parsed = urllib.parse.urlparse(self.base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("MODEL_BASE_URL must be HTTPS without embedded credentials")
        self.name = os.getenv("MODEL_NAME", "deepseek-flash")
        self.timeout = float(os.getenv("MODEL_TIMEOUT_SECONDS", "45"))
        if not 1 <= self.timeout <= 120:
            raise ValueError("MODEL_TIMEOUT_SECONDS must be 1..120")
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def decide(self, messages, schemas):
        body = {"model": self.name, "messages": messages, "tools": schemas,
                "tool_choice": "required",
                "thinking": {"type": "disabled"}, "temperature": 0,
                "max_tokens": 1024, "stream": False}
        req = urllib.request.Request(self.base + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key})
        # Do not follow redirects carrying authentication to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=self.timeout) as resp:
                raw = resp.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ModelError("model_response_too_large")
            data = json.loads(raw)
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ModelError("model_response_truncated")
            calls = choice["message"].get("tool_calls", [])
            if len(calls) != 1:
                raise ModelError("model_must_return_exactly_one_tool_call")
            call = calls[0]
            args = json.loads(call["function"]["arguments"])
            if not isinstance(args, dict) or not isinstance(call.get("id"), str):
                raise ModelError("invalid_model_tool_call")
            for key in self.usage:
                self.usage[key] += int(data.get("usage", {}).get(key, 0))
            self.resolved_name = data.get("model", self.name)
            return Decision(call["function"]["name"], args, call["id"])
        except urllib.error.HTTPError as exc:
            # Never emit provider response bodies: they may echo credentials or private input.
            raise ModelError(f"model_http_{exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ModelError("model_network_or_timeout_error") from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("invalid_model_response") from None


class MockModel:
    """Scripted policy over observations. It proves workflow, never LLM quality."""
    kind = "mock"
    name = "deterministic-offline-policy-v1"

    def decide(self, messages, schemas):
        request = json.loads(messages[1]["content"])["request"]
        history = []
        for i, message in enumerate(messages):
            if message["role"] == "tool":
                call = messages[i - 1]["tool_calls"][0]["function"]
                history.append((call["name"], json.loads(call["arguments"]),
                                json.loads(message["content"])))
        def decision(name, mid=None, reason="依据工具结果完成本步骤。", **args):
            if mid:
                args["material_id"] = mid
            return Decision(name, {**args, "reason": reason}, "mock_" + str(len(history) + 1))
        for mid in request["material_ids"]:
            events = [(n, a, o) for n, a, o in history if a.get("material_id") == mid]
            if any((n == "save_draft" and o.get("ok")) or
                   (n == "close_item" and o.get("ok")) for n, _, o in events):
                continue
            if not events:
                return decision("get_material", mid, "先读取素材事实。")
            name, args, obs = events[-1]
            if name == "get_material" and not obs.get("ok"):
                return decision("close_item", mid, "读取工具失败，明确停止。", outcome="tool_failure")
            if name in {"get_material", "lookup_metadata"}:
                return decision("check_rules", mid, "根据当前元数据核对规则。")
            if name == "check_rules":
                status = obs.get("status")
                if status == "needs_information" and not any(n == "lookup_metadata" for n, _, _ in events):
                    return decision("lookup_metadata", mid, "检测到缺失字段，尝试可信资料源补查。")
                if status in {"needs_information", "blocked", "human_review"}:
                    return decision("close_item", mid, "规则检查未通过，保留待处理项。", outcome=status)
                return decision("query_slots", mid, "规则通过，检查已有排期和草稿占位。")
            if name == "query_slots":
                if not obs.get("ok"):
                    return decision("close_item", mid, "时段工具失败，不能猜测可用时间。", outcome="tool_failure")
                available = [x for x in obs["candidates"] if x["available"]]
                if not available:
                    return decision("close_item", mid, "查询窗口无可用时段，需要用户调整要求。", outcome="no_slot")
                at = available[0]["at"]
                reason = ("原时段可用，保存草稿。" if at == request["requested_at"] else
                          "原时段冲突，选择可用替代时段，确认状态交由保存工具校验。")
                return decision("save_draft", mid, reason, proposed_at=at)
            if name == "save_draft" and not obs.get("ok"):
                return decision("close_item", mid, "保存未成功，明确返回失败。", outcome="tool_failure")
            raise ModelError("mock_policy_unhandled_observation")
        return decision("finish", reason="每项素材均有工具支持的最终处理结果。")
