"""One user turn at a time: model, tools, results, then a final answer."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from .context import assemble
from .model import ModelError, Reply, ToolCall
from .storage import Store
from .tools import ToolError, ToolRegistry


class Model(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> Reply: ...


@dataclass(frozen=True)
class Limits:
    model_calls: int = 6
    tool_calls: int = 8
    history_chars: int = 12000


@dataclass(frozen=True)
class Outcome:
    run_id: int
    status: str
    message: str
    effects: tuple[str, ...]


class Agent:
    def __init__(
        self, store: Store, model: Model, registry: ToolRegistry, limits: Limits,
        trace: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.model = model
        self.registry = registry
        self.limits = limits
        self.trace = trace

    def run(self, session_id: int, text: str) -> Outcome:
        run_id = self.store.start_run(session_id, text)
        requests = 0
        attempted_ids: set[str] = set()
        schemas = self.registry.schemas()

        def request(messages: list[dict], tools: list[dict]) -> Reply:
            nonlocal requests
            requests += 1
            return self.model.complete(messages, tools)

        def summarize(messages: list[dict]) -> str:
            reply = request(messages, [])
            if reply.tool_calls or not reply.content:
                raise ModelError("模型未返回可用的会话摘要")
            return reply.content

        def finish(status: str, message: str) -> Outcome:
            self.store.finish_run(run_id, status, message if status == "completed" else None)
            return Outcome(run_id, status, message, tuple(self.store.effects(run_id)))

        try:
            messages = assemble(
                self.store, session_id, text, self.limits.history_chars,
                summarize if self.limits.model_calls > 1 else None,
            )
            while True:
                reply = request(messages, schemas)
                if not reply.tool_calls:
                    if not reply.content:
                        raise ModelError("模型未返回可用的最终答复")
                    return finish("completed", reply.content)

                call_ids = {call.id for call in reply.tool_calls}
                if len(call_ids) != len(reply.tool_calls) or attempted_ids & call_ids:
                    raise ModelError("模型返回了重复的工具调用 ID")
                if requests >= self.limits.model_calls:
                    return finish("limited", "已达到模型请求上限，当前轮结束")
                if len(attempted_ids) + len(reply.tool_calls) > self.limits.tool_calls:
                    return finish("limited", "剩余工具调用次数不足以执行整个批次，当前轮结束")

                messages.append({
                    "role": "assistant",
                    "content": reply.content,
                    "tool_calls": [call.as_message_value() for call in reply.tool_calls],
                })
                for call in reply.tool_calls:
                    attempted_ids.add(call.id)
                    result = self._execute(session_id, run_id, len(attempted_ids), call)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
        except ModelError as error:
            return finish("failed", str(error))
        except KeyboardInterrupt:
            return finish("interrupted", "当前轮已中断")

    def _execute(self, session_id: int, run_id: int, position: int, call: ToolCall) -> dict:
        started = perf_counter()
        try:
            with self.store.db:
                try:
                    result = self.registry.execute(self.store, session_id, call)
                except ToolError:
                    raise
                except Exception:
                    raise ToolError("tool_execution_failed", "工具执行失败") from None
                payload = {"ok": True, "data": result.data}
                elapsed = round((perf_counter() - started) * 1000)
                self.store.record_tool(
                    run_id, position, call.id, call.name, elapsed, True, payload, result.effect,
                )
        except ToolError as error:
            elapsed = round((perf_counter() - started) * 1000)
            payload = {"ok": False, "error": {"code": error.code, "message": str(error)}}
            with self.store.db:
                self.store.record_tool(
                    run_id, position, call.id, call.name, elapsed, False, payload, None,
                )
        if self.trace:
            status = "成功" if payload["ok"] else "失败"
            self.trace(f"[会话 {session_id} / 运行 {run_id} / 工具 {position}] {call.name} {status} {elapsed} ms")
        return payload
