"""Tool registration, validation, and local tool implementations."""

import ast
import json
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, DecimalException

from .model import ToolCall
from .storage import Store


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolResult:
    data: dict
    effect: str | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict
    required: tuple[str, ...]
    handler: Callable[[Store, int, dict], ToolResult]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self.tools:
            raise ValueError(f"重复注册工具：{tool.name}")
        self.tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self.tools.values()]

    def execute(self, store: Store, session_id: int, call: ToolCall) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            raise ToolError("unknown_tool", "工具未注册")
        try:
            values = json.loads(call.arguments)
        except (ValueError, RecursionError):
            raise ToolError("invalid_arguments", "工具参数不是有效的 JSON") from None
        if not isinstance(values, dict) or values.keys() - tool.properties.keys():
            raise ToolError("invalid_arguments", "工具参数必须是对象且只包含已定义字段")
        if set(tool.required) - values.keys():
            raise ToolError("invalid_arguments", "工具缺少必要参数")
        types = {"string": str, "integer": int}
        for field, value in values.items():
            spec = tool.properties[field]
            if type(value) is not types[spec["type"]]:
                raise ToolError("invalid_arguments", f"字段 {field} 类型错误")
            if "enum" in spec and value not in spec["enum"]:
                raise ToolError("invalid_arguments", f"字段 {field} 的值不受支持")
        return tool.handler(store, session_id, values)


BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
}
UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
SYMBOLS = str.maketrans({"×": "*", "÷": "/", "−": "-"})


def _decimal_value(node: ast.expr, source: str) -> Decimal:
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        return Decimal(source[node.col_offset:node.end_col_offset])
    if isinstance(node, ast.BinOp) and type(node.op) in BINARY:
        return BINARY[type(node.op)](_decimal_value(node.left, source), _decimal_value(node.right, source))
    if isinstance(node, ast.UnaryOp) and type(node.op) in UNARY:
        return UNARY[type(node.op)](_decimal_value(node.operand, source))
    raise ToolError("invalid_expression", "仅支持整数、小数、正负号、四则运算和括号")


def calculator(_store: Store, _session_id: int, values: dict) -> ToolResult:
    source = values["expression"].translate(SYMBOLS).strip()
    if len(source) > 256 or not re.fullmatch(r"[0-9.+*/() \t-]+", source):
        raise ToolError("invalid_expression", "请输入不超过 256 字符的四则运算表达式")
    try:
        expression = ast.parse(source, mode="eval").body
        result = _decimal_value(expression, source)
    except (SyntaxError, RecursionError):
        raise ToolError("invalid_expression", "四则运算表达式无效") from None
    except DecimalException:
        raise ToolError("invalid_arithmetic", "无法完成运算，请检查除数和数值范围") from None
    return ToolResult({"value": format(result, "f")})


EVENTS = (
    {"name": "城南读书会", "date": "2026-12-05", "time": "14:30", "location": "青禾社区活动室"},
    {"name": "海风影展", "date": "2026-12-06", "time": "19:00", "location": "白石放映厅"},
    {"name": "春水手作课", "date": "2026-12-12", "time": "10:00", "location": "木桥工坊"},
)


def search(_store: Store, _session_id: int, values: dict) -> ToolResult:
    keywords = values["query"].casefold().split()
    if not keywords or len(values["query"]) > 120:
        raise ToolError("invalid_arguments", "搜索关键词长度须在 1 到 120 字符之间")
    matches = [
        event for event in EVENTS
        if all(word in " ".join(event.values()).casefold() for word in keywords)
    ]
    return ToolResult({"source": "模拟数据（虚构活动）", "results": matches})


def todo(store: Store, session_id: int, values: dict) -> ToolResult:
    action = values["action"]
    if action == "list":
        if set(values) != {"action"}:
            raise ToolError("invalid_arguments", "列出待办只需要 action")
        return ToolResult({"action": action, "items": store.list_todos(session_id)})
    if action == "add":
        title = values.get("title", "").strip()
        if not title or len(title) > 240 or "id" in values:
            raise ToolError("invalid_arguments", "新增待办需要 1 到 240 字符的标题")
        item = store.add_todo(session_id, title)
        return ToolResult({"action": action, "item": item}, f"已新增待办 #{item['id']}")

    item_id = values.get("id", 0)
    if not 0 < item_id <= 2**63 - 1 or "title" in values:
        raise ToolError("invalid_arguments", "标记完成需要有效的待办编号")
    item, changed = store.complete_todo(session_id, item_id)
    if item is None:
        raise ToolError("todo_not_found", "当前会话没有该编号的待办")
    effect = f"已完成待办 #{item_id}" if changed else None
    return ToolResult({"action": action, "item": item, "changed": changed}, effect)


def default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        "calculator", "计算四则运算表达式。",
        {"expression": {"type": "string", "description": "例如 -(2 + 3.5) * 4"}},
        ("expression",), calculator,
    ))
    registry.register(Tool(
        "search", "按简短关键词搜索固定的虚构活动数据，可查活动日期、时间和地点。",
        {"query": {"type": "string", "description": "活动名称或简短关键词，例如城南读书会"}},
        ("query",), search,
    ))
    registry.register(Tool(
        "todo", "管理当前会话待办。add 需要 title，list 只需 action，complete 需要 id。",
        {
            "action": {"type": "string", "enum": ["add", "list", "complete"]},
            "title": {"type": "string", "description": "新增待办的标题"},
            "id": {"type": "integer", "description": "要标记完成的待办编号"},
        },
        ("action",), todo,
    ))
    return registry
