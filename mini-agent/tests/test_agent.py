import copy
import io
import json
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from mini_agent.model import ChatClient, ModelError, Reply, ToolCall, parse_reply
from mini_agent.runtime import Agent, Limits
from mini_agent.storage import Store
from mini_agent.tools import default_registry


def call(call_id, name, **arguments):
    return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


def result(messages, call_id):
    return next(
        json.loads(message["content"])
        for message in messages
        if message.get("tool_call_id") == call_id
    )


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = deque(replies)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        reply = self.replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


class AgentTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "test.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.session = self.store.create_session("甲")

    def agent(self, *replies, limits=Limits(), trace=None):
        model = ScriptedModel(*replies)
        return Agent(self.store, model, default_registry(), limits, trace), model

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.store.recover_interrupted()

    def test_direct_answer_and_plain_followup(self):
        agent, model = self.agent(Reply("记住了，小林"), Reply("你叫小林"))
        self.assertEqual(agent.run(self.session, "我叫小林").status, "completed")
        self.assertEqual(agent.run(self.session, "我叫什么？").message, "你叫小林")
        history, schemas = model.requests[-1]
        self.assertEqual([message["content"] for message in history[1:]], [
            "我叫小林", "记住了，小林", "我叫什么？",
        ])
        self.assertEqual({tool["function"]["name"] for tool in schemas}, {"calculator", "search", "todo"})

    def test_multi_step_search_to_todo_and_followup(self):
        lines = []
        agent, model = self.agent(
            Reply(None, (call("search-id", "search", query="城南读书会"),)),
            Reply(None, (call("add-id", "todo", action="add", title="14:30 城南读书会"),)),
            Reply("已添加 #1"),
            Reply(None, (call("list-id", "todo", action="list"),)),
            Reply("你有一条待办"),
            trace=lines.append,
        )
        self.assertEqual(agent.run(self.session, "查读书会时间并记待办").status, "completed")
        self.assertEqual(agent.run(self.session, "我还有什么待办？").status, "completed")
        search_result = result(model.requests[1][0], "search-id")["data"]
        self.assertIn("模拟", search_result["source"])
        self.assertEqual(search_result["results"][0]["time"], "14:30")
        self.assertEqual(result(model.requests[2][0], "add-id")["data"]["item"]["id"], 1)
        self.assertEqual(result(model.requests[4][0], "list-id")["data"]["items"][0]["title"], "14:30 城南读书会")
        self.assertEqual([line.split("] ")[1].split()[0] for line in lines], ["search", "todo", "todo"])
        self.assertIn("历史工具结果", model.requests[3][0][-2]["content"])

    def test_batch_order_matters_for_mutation_then_read(self):
        agent, model = self.agent(
            Reply(None, (call("first", "todo", action="add", title="报告"), call("second", "todo", action="list"))),
            Reply("已处理"),
        )
        self.assertEqual(agent.run(self.session, "新增并列出").status, "completed")
        messages = model.requests[1][0]
        self.assertEqual([message["tool_call_id"] for message in messages[-2:]], ["first", "second"])
        self.assertEqual(result(messages, "second")["data"]["items"][0]["title"], "报告")

    def test_calculator_and_unmatched_search(self):
        agent, model = self.agent(
            Reply(None, (
                call("calc", "calculator", expression="-(2 + 3.5) × 4"),
                call("miss", "search", query="完全不存在"),
            )),
            Reply("-22，没有匹配的活动"),
        )
        self.assertEqual(agent.run(self.session, "算完后查活动").status, "completed")
        self.assertEqual(result(model.requests[1][0], "calc")["data"]["value"], "-22.0")
        self.assertEqual(result(model.requests[1][0], "miss")["data"]["results"], [])

    def test_structured_errors_and_correction(self):
        agent, model = self.agent(
            Reply(None, (
                ToolCall("malformed", "calculator", "{"),
                call("zero", "calculator", expression="1 / 0"),
                call("syntax", "calculator", expression="(3 +"),
                call("unknown", "unknown_tool"),
            )),
            Reply(None, (call("correct", "calculator", expression="1 / 2"),)),
            Reply("0.5"),
        )
        self.assertEqual(agent.run(self.session, "计算").message, "0.5")
        messages = model.requests[1][0]
        self.assertEqual(
            [result(messages, ident)["error"]["code"] for ident in ("malformed", "zero", "syntax", "unknown")],
            ["invalid_arguments", "invalid_arithmetic", "invalid_expression", "unknown_tool"],
        )

    def test_session_isolation_persists_and_todo_id_is_scoped(self):
        other = self.store.create_session("乙")
        agent, _ = self.agent(
            Reply(None, (call("create", "todo", action="add", title="甲的事"),)),
            Reply("甲已保存"),
            Reply("乙的对话"),
        )
        agent.run(self.session, "甲的指令")
        agent.run(other, "乙的指令")
        self.reopen()
        second, model = self.agent(
            Reply(None, (call("wrong", "todo", action="complete", id=1),)),
            Reply("乙没有此编号"),
            Reply(None, (call("right", "todo", action="complete", id=1),)),
            Reply("甲已完成"),
        )
        second.run(other, "完成待办 #1")
        self.assertNotIn("甲的指令", json.dumps(model.requests[0][0], ensure_ascii=False))
        self.assertEqual(result(model.requests[1][0], "wrong")["error"]["code"], "todo_not_found")
        self.assertFalse(self.store.list_todos(self.session)[0]["done"])
        second.run(self.session, "完成待办 #1")
        self.assertIn("甲的指令", json.dumps(model.requests[2][0], ensure_ascii=False))
        self.assertTrue(self.store.list_todos(self.session)[0]["done"])
        self.assertEqual(self.store.list_todos(other), [])

    def test_unexpected_tool_error_rolls_back_mutation_and_is_redacted(self):
        agent, model = self.agent(
            Reply(None, (call("create", "todo", action="add", title="报告"),)),
            Reply("无法保存"),
        )
        add = self.store.add_todo

        def write_then_fail(session_id, title):
            add(session_id, title)
            raise OSError("内部敏感错误")

        with patch.object(self.store, "add_todo", side_effect=write_then_fail):
            self.assertEqual(agent.run(self.session, "记报告").status, "completed")
        error = result(model.requests[1][0], "create")["error"]
        self.assertEqual(error["code"], "tool_execution_failed")
        self.assertNotIn("内部敏感错误", error["message"])
        self.assertEqual(self.store.list_todos(self.session), [])

    def test_storage_failure_cannot_commit_todo_without_tool_event(self):
        agent, _ = self.agent(Reply(None, (call("create", "todo", action="add", title="报告"),)))
        with patch.object(self.store, "record_tool", side_effect=OSError):
            with self.assertRaises(OSError):
                agent.run(self.session, "记报告")
        self.reopen()
        self.assertEqual(self.store.list_todos(self.session), [])
        self.assertEqual(self.store.effects(1), [])

    def test_model_failure_reports_effect_and_excludes_failed_turn(self):
        agent, model = self.agent(
            Reply(None, (call("create", "todo", action="add", title="报告"),)),
            ModelError("模型服务超时"),
            Reply("可以继续"),
        )
        failed = agent.run(self.session, "中途将失败的请求")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.effects, ("已新增待办 #1",))
        self.assertEqual(len(self.store.list_todos(self.session)), 1)
        self.assertEqual(agent.run(self.session, "继续").status, "completed")
        self.assertNotIn("中途将失败的请求", json.dumps(model.requests[-1][0], ensure_ascii=False))

    def test_limit_stops_before_writing_without_followup(self):
        agent, model = self.agent(
            Reply(None, (call("create", "todo", action="add", title="报告"),)),
            limits=Limits(model_calls=1),
        )
        self.assertEqual(agent.run(self.session, "记报告").status, "limited")
        self.assertEqual(self.store.list_todos(self.session), [])
        self.assertEqual(len(model.requests), 1)

    def test_batch_limit_counts_failed_attempts_and_preserves_previous_effects(self):
        agent, _ = self.agent(
            Reply(None, (
                call("unknown", "unknown_tool"),
                call("add", "todo", action="add", title="先做"),
            )),
            Reply(None, (
                call("would-fit", "todo", action="add", title="再做"),
                call("would-overflow", "todo", action="list"),
            )),
            limits=Limits(tool_calls=3),
        )
        outcome = agent.run(self.session, "办两件事")
        self.assertEqual(outcome.status, "limited")
        self.assertEqual(outcome.effects, ("已新增待办 #1",))
        self.assertEqual([item["title"] for item in self.store.list_todos(self.session)], ["先做"])

    def _seed_history(self):
        for number in range(6):
            run_id = self.store.start_run(self.session, f"事实{number}" + "甲" * 35)
            self.store.finish_run(run_id, "completed", f"回答{number}" + "乙" * 35)

    def test_compaction_retains_history_and_places_summary_before_recent_turns(self):
        self._seed_history()
        agent, model = self.agent(Reply("早期事实0是甲"), Reply("早期事实是甲"), limits=Limits(history_chars=420))
        self.assertEqual(agent.run(self.session, "早期事实是什么？").message, "早期事实是甲")
        summary_request, tools = model.requests[0]
        self.assertEqual(tools, [])
        self.assertIn("事实0", summary_request[1]["content"])
        messages = model.requests[1][0]
        self.assertIn("早期事实0是甲", messages[1]["content"])
        self.assertIn("事实5", messages[-3]["content"])
        self.assertLessEqual(sum(len(message["content"]) for message in messages[1:-1]), 420)
        self.assertEqual(len(self.store.completed_turns(self.session, 0)), 7)
        self.assertGreater(self.store.session(self.session)["summarized_through"], 0)

    def test_compaction_spends_from_the_model_budget(self):
        self._seed_history()
        agent, model = self.agent(
            Reply("摘要"),
            Reply(None, (call("add", "todo", action="add", title="报告"),)),
            limits=Limits(model_calls=2, history_chars=420),
        )
        self.assertEqual(agent.run(self.session, "记报告").status, "limited")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(self.store.list_todos(self.session), [])

    def test_parser_reads_optional_reasoning_and_native_calls(self):
        native = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "reasoning_content": "离线构造的推理字段",
                    "tool_calls": [call("native", "calculator", expression="2+2").as_message_value()],
                },
            }],
        }
        reply = parse_reply(native)
        self.assertEqual(reply.reasoning_content, "离线构造的推理字段")
        self.assertEqual(reply.tool_calls[0].id, "native")
        no_reasoning = {"choices": [{"finish_reason": "stop", "message": {"content": "4"}}]}
        self.assertIsNone(parse_reply(no_reasoning).reasoning_content)
        broken = {"choices": [{"finish_reason": "length", "message": {"content": "截断"}}]}
        with self.assertRaisesRegex(ModelError, "长度"):
            parse_reply(broken)


class AdapterTests(unittest.TestCase):
    def test_deepseek_can_read_saved_environment_when_process_has_no_key(self):
        with patch.dict(os.environ, {"AGENT_BASE_URL": "https://api.deepseek.com"}, clear=True):
            with patch("mini_agent.model._persisted_api_key", return_value="saved-test-key") as read_saved:
                client = ChatClient.from_env()
        self.assertEqual(client.model, "deepseek-flash")
        self.assertEqual(client.api_key, "saved-test-key")
        read_saved.assert_called_once_with("DEEPSEEK_API_KEY")

    def test_deepseek_prefers_credentials_from_the_process(self):
        env = {"AGENT_BASE_URL": "https://api.deepseek.com", "DEEPSEEK_API_KEY": "process-test-key"}
        with patch.dict(os.environ, env, clear=True):
            with patch("mini_agent.model._persisted_api_key") as read_saved_key:
                client = ChatClient.from_env()
        self.assertEqual(client.api_key, "process-test-key")
        read_saved_key.assert_not_called()

    def test_other_endpoints_never_receive_the_deepseek_key_by_fallback(self):
        env = {
            "AGENT_BASE_URL": "https://other.example/v1",
            "DEEPSEEK_API_KEY": "process-test-key",
            "ZHIPUAI_API_KEY": "offline-zhipu-key",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("mini_agent.model._persisted_api_key") as read_saved_key:
                with self.assertRaisesRegex(ModelError, "AGENT_API_KEY"):
                    ChatClient.from_env()
        read_saved_key.assert_not_called()

    def test_zhipu_is_the_default_and_reads_its_saved_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("mini_agent.model._persisted_api_key", return_value="saved-zhipu-key") as read_saved:
                client = ChatClient.from_env()
        self.assertEqual(client.endpoint, "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertEqual(client.model, "glm-4.7-flashx")
        self.assertEqual(client.api_key, "saved-zhipu-key")
        read_saved.assert_called_once_with("ZHIPUAI_API_KEY")

    def test_switch_does_not_send_previous_credentials_to_the_new_default(self):
        env = {"DEEPSEEK_API_KEY": "previous-key", "AGENT_API_KEY": "unbound-key"}
        with patch.dict(os.environ, env, clear=True):
            with patch("mini_agent.model._persisted_api_key", return_value="") as read_saved:
                with self.assertRaisesRegex(ModelError, "ZHIPUAI_API_KEY"):
                    ChatClient.from_env()
        self.assertEqual(
            [invocation.args[0] for invocation in read_saved.call_args_list],
            ["ZHIPUAI_API_KEY", "ZHIPU_API_KEY"],
        )

    def test_zhipu_native_tool_roundtrip_without_thinking(self):
        with patch.dict(os.environ, {"ZHIPUAI_API_KEY": "offline-zhipu-key"}, clear=True):
            client = ChatClient.from_env()
        search_call = call("glm-search", "search", query="城南读书会")
        payloads = [
            {"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [search_call.as_message_value()],
            }}]},
            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "14:30"}}]},
        ]
        store = Store(":memory:")
        self.addCleanup(store.close)
        session = store.create_session("甲")
        replies = [io.BytesIO(json.dumps(payload).encode()) for payload in payloads]
        with patch.object(client.opener, "open", side_effect=replies) as send:
            outcome = Agent(store, client, default_registry(), Limits()).run(session, "城南读书会几点开始？")
        self.assertEqual(outcome.message, "14:30")
        first, second = [json.loads(invocation.args[0].data) for invocation in send.call_args_list]
        self.assertEqual(first["model"], "glm-4.7-flashx")
        self.assertEqual(first["thinking"], {"type": "disabled"})
        self.assertEqual(first["tool_choice"], "auto")
        self.assertEqual(result(second["messages"], "glm-search")["data"]["results"][0]["time"], "14:30")

    def test_zhipu_accepts_zhipu_api_key_as_an_alias(self):
        with patch.dict(os.environ, {"ZHIPU_API_KEY": "alias-key"}, clear=True):
            with patch("mini_agent.model._persisted_api_key") as read_saved:
                client = ChatClient.from_env()
        self.assertEqual(client.api_key, "alias-key")
        read_saved.assert_not_called()

    def test_zhipu_reports_model_capacity_error(self):
        client = ChatClient("https://open.bigmodel.cn/api/paas/v4", "glm-4.7-flash", "offline-token")
        body = b'{"error":{"code":"1305","message":"internal detail"}}'
        error = HTTPError(client.endpoint, 429, "Too Many Requests", {}, io.BytesIO(body))
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaisesRegex(ModelError, "访问量过大.*1305") as caught:
                client.complete([], [])
        self.assertNotIn("internal detail", str(caught.exception))

    def test_deepseek_request_disables_thinking(self):
        client = ChatClient("https://api.deepseek.com", "deepseek-flash", "offline-token")
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": "明白"}}]}
        with patch.object(client.opener, "open", return_value=io.BytesIO(json.dumps(payload).encode())) as send:
            reply = client.complete([{"role": "user", "content": "你好"}], default_registry().schemas())
        request = send.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(reply.content, "明白")

    def test_http_failure_uses_a_redacted_message(self):
        client = ChatClient("https://api.deepseek.com", "deepseek-flash", "offline-token")
        errors = ((401, "鉴权失败"), (402, "可用余额不足"), (429, "限流"), (502, "HTTP 502"))
        for status, message in errors:
            with self.subTest(status=status):
                error = HTTPError(client.endpoint, status, "内部响应", {}, io.BytesIO(b"internal detail"))
                with patch.object(client.opener, "open", side_effect=error):
                    with self.assertRaisesRegex(ModelError, message) as caught:
                        client.complete([], [])
                self.assertNotIn("internal detail", str(caught.exception))
