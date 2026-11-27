"""Run the interactive command-line agent."""

import argparse
import sqlite3
import sys
from pathlib import Path

if not __package__:
    # IDEs can run this file directly with only mini_agent/ on the import path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mini_agent.model import ChatClient, ModelError
from mini_agent.runtime import Agent, Limits
from mini_agent.storage import Store
from mini_agent.tools import default_registry


HELP = """/new [名称]   创建并切换到会话
/sessions     列出会话
/switch 编号  切换会话
/exit         退出
/help         显示命令"""


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def show_interruptions(store: Store, session_id: int):
    for interrupted in store.pending_interruptions(session_id):
        run_id = interrupted["id"]
        print(f"运行 {run_id} 上次中断，已恢复到最后一轮完整对话。")
        for effect in store.effects(run_id):
            print(f"  {effect}")
        store.acknowledge_interruption(run_id)


def interactive(store: Store, agent: Agent):
    store.recover_interrupted()
    sessions = store.list_sessions()
    session_id = sessions[-1]["id"] if sessions else store.create_session("默认会话")
    print("Mini Agent · 输入 /help 查看命令")
    print(f"当前会话 #{session_id} {store.session(session_id)['name']}")
    show_interruptions(store, session_id)

    while True:
        try:
            text = input(f"[会话 {session_id}] 你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text.startswith("/"):
            command, _, argument = text.partition(" ")
            argument = argument.strip()
            if command == "/exit":
                return
            if command == "/help":
                print(HELP)
            elif command == "/sessions":
                for session in store.list_sessions():
                    marker = "*" if session["id"] == session_id else " "
                    print(f"{marker} #{session['id']} {session['name']}")
            elif command == "/new":
                session_id = store.create_session(argument or "未命名会话")
                print(f"已创建会话 #{session_id}")
            elif command == "/switch":
                if not argument.isascii() or not argument.isdecimal() or len(argument) > 19:
                    print("请输入有效的会话编号")
                    continue
                target = int(argument)
                session = store.session(target) if target <= 2**63 - 1 else None
                if session is None:
                    print("会话不存在")
                    continue
                session_id = target
                print(f"已切换到会话 #{session_id} {session['name']}")
                show_interruptions(store, session_id)
            else:
                print("未知命令，输入 /help 查看可用命令")
            continue

        outcome = agent.run(session_id, text)
        if outcome.status == "completed":
            print(f"Agent> {outcome.message}")
        else:
            print(f"状态> {outcome.message}")
            for effect in outcome.effects:
                print(f"  {effect}")
            if outcome.status == "interrupted":
                store.acknowledge_interruption(outcome.run_id)


def main() -> int:
    defaults = Limits()
    parser = argparse.ArgumentParser(description="带本地会话的最小工具调用 Agent")
    parser.add_argument("--db", type=Path, default=Path("agent.sqlite3"), help="本地 SQLite 文件")
    parser.add_argument("--trace", action="store_true", help="显示工具调用执行记录")
    parser.add_argument("--max-model-calls", type=positive, default=defaults.model_calls)
    parser.add_argument("--max-tool-calls", type=positive, default=defaults.tool_calls)
    parser.add_argument("--history-chars", type=positive, default=defaults.history_chars)
    args = parser.parse_args()
    limits = Limits(args.max_model_calls, args.max_tool_calls, args.history_chars)

    try:
        client = ChatClient.from_env()
        store = Store(args.db)
        try:
            agent = Agent(store, client, default_registry(), limits, print if args.trace else None)
            interactive(store, agent)
        finally:
            store.close()
    except ModelError as error:
        parser.exit(2, f"配置错误：{error}\n")
    except sqlite3.Error:
        parser.exit(1, "无法访问本地 SQLite 会话数据，请检查文件路径与可用空间。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
