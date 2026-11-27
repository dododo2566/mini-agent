"""Chat Completions transport and response parsing."""

import json
import os
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ZHIPU_HOST = "open.bigmodel.cn"
ZHIPU_MODEL = "glm-4.7-flashx"
ZHIPU_FREE_MODEL = "glm-4.7-flash"
DEEPSEEK_HOST = "api.deepseek.com"
DEFAULT_BASE_URL = f"https://{ZHIPU_HOST}/api/paas/v4"
PROVIDERS = {
    ZHIPU_HOST: (ZHIPU_MODEL, ("ZHIPUAI_API_KEY", "ZHIPU_API_KEY")),
    DEEPSEEK_HOST: ("deepseek-flash", ("DEEPSEEK_API_KEY",)),
}
ZHIPU_429_ERRORS = {
    "1113": "智谱 API 账户已欠费（HTTP 429，业务码 1113）",
    "1302": "智谱 API 账户达到速率限制（HTTP 429，业务码 1302）",
    "1305": "智谱模型当前访问量过大，请稍后再试（HTTP 429，业务码 1305）",
}


class ModelError(Exception):
    """A safe, user-facing model or transport error."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def as_message_value(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class Reply:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = None


def parse_reply(data: dict) -> Reply:
    try:
        choice = data["choices"][0]
        message = choice["message"]
        reason = choice["finish_reason"]
        content = message.get("content")
        reasoning = message.get("reasoning_content")
        raw_calls = message.get("tool_calls")
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list):
            raise TypeError
        if not isinstance(reason, str):
            raise TypeError
        calls = []
        for raw in raw_calls:
            if raw["type"] != "function":
                raise ValueError
            call = ToolCall(raw["id"], raw["function"]["name"], raw["function"]["arguments"])
            if not all(isinstance(value, str) for value in (call.id, call.name, call.arguments)):
                raise TypeError
            if not call.id or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", call.name):
                raise ValueError
            calls.append(call)
        if len({call.id for call in calls}) != len(calls):
            raise ValueError
        if content is not None and not isinstance(content, str):
            raise TypeError
        if reasoning is not None and not isinstance(reasoning, str):
            raise TypeError
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        raise ModelError("模型响应格式无效") from None

    if reason == "length":
        raise ModelError("模型输出达到长度限制")
    if reason not in {"stop", "tool_calls"}:
        raise ModelError("模型未正常完成响应")
    if (reason == "tool_calls") != bool(calls) or (not calls and not (content or "").strip()):
        raise ModelError("模型响应缺少可用的答复或工具调用")
    return Reply(content, tuple(calls), reasoning)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _persisted_api_key(name: str) -> str:
    if os.name != "nt":
        return ""

    import winreg

    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for hive, path in locations:
        try:
            with winreg.OpenKey(hive, path, access=winreg.KEY_QUERY_VALUE) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _provider_api_key(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for name in names:
        if name not in os.environ:
            value = _persisted_api_key(name)
            if value:
                return value
    return ""


def _zhipu_429_message(error: HTTPError) -> str:
    try:
        code = str(json.loads(error.read(4096))["error"]["code"])
    except (KeyError, TypeError, ValueError, OSError):
        code = ""
    return ZHIPU_429_ERRORS.get(code, "智谱模型服务暂不可用（HTTP 429）")


class ChatClient:
    def __init__(self, base_url: str, model: str | None, api_key: str, timeout: float = 45):
        try:
            parts = urlsplit(base_url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            ):
                raise ValueError
            if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError
        except ValueError:
            raise ModelError("模型服务地址无效，使用 HTTPS 或本机 HTTP") from None

        self.hostname = parts.hostname
        default_model, self.provider_keys = PROVIDERS.get(self.hostname, (ZHIPU_FREE_MODEL, ()))
        self.model = default_model if model is None else model
        if not self.model:
            raise ModelError("AGENT_MODEL 缺少模型名")
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.timeout = timeout
        self.opener = build_opener(NoRedirects())

    @classmethod
    def from_env(cls):
        configured_url = os.environ.get("AGENT_BASE_URL")
        base_url = DEFAULT_BASE_URL if configured_url is None else configured_url.strip()
        model = os.environ.get("AGENT_MODEL")
        # A generic credential must be paired with an explicit destination.
        api_key = os.environ.get("AGENT_API_KEY", "").strip() if configured_url is not None else ""
        client = cls(base_url, model.strip() if model is not None else None, api_key)
        if not client.api_key:
            client.api_key = _provider_api_key(client.provider_keys)
        if not client.api_key:
            if client.provider_keys:
                raise ModelError(f"请通过环境变量设置 {client.provider_keys[0]}")
            raise ModelError("请通过环境变量为当前模型服务设置 AGENT_API_KEY")
        return client

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        body = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.hostname == DEEPSEEK_HOST or (
            self.hostname == ZHIPU_HOST and self.model.casefold() in {ZHIPU_MODEL, ZHIPU_FREE_MODEL}
        ):
            body["thinking"] = {"type": "disabled"}

        request = Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = json.load(response)
        except HTTPError as error:
            code = error.code
            if code == 429 and self.hostname == ZHIPU_HOST:
                with error:
                    message = _zhipu_429_message(error)
                raise ModelError(message) from None
            error.close()
            if code in {401, 403}:
                raise ModelError("模型服务鉴权失败") from None
            if code == 402 and self.hostname == DEEPSEEK_HOST:
                raise ModelError("DeepSeek API 账户可用余额不足（HTTP 402）") from None
            if code == 429:
                raise ModelError("模型服务限流") from None
            raise ModelError(f"模型服务返回 HTTP {code}") from None
        except TimeoutError:
            raise ModelError("模型服务超时") from None
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise ModelError("模型服务超时") from None
            raise ModelError("无法连接模型服务") from None
        except OSError:
            raise ModelError("读取模型服务响应失败") from None
        except (ValueError, UnicodeError):
            raise ModelError("模型服务返回了无效的 JSON") from None
        return parse_reply(data)
