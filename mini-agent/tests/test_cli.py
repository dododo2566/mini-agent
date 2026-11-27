import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def response(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def write_todo():
    return response(calls=[{
        "id": "offline-add",
        "type": "function",
        "function": {"name": "todo", "arguments": '{"action":"add","title":"测试待办"}'},
    }])


class CLITests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(directory) / "cli.sqlite3"

    def server(self, handler):
        class LocalHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                output = json.dumps(handler(self.path, body), ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(output)))
                    self.end_headers()
                    self.wfile.write(output)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args):
                pass

        service = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()

        def stop():
            service.shutdown()
            service.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        env = os.environ.copy()
        env.pop("DEEPSEEK_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        env.update({
            "AGENT_BASE_URL": f"http://127.0.0.1:{service.server_port}/v1",
            "AGENT_MODEL": "local-test",
            "AGENT_API_KEY": "offline-test-token",
            "PYTHONIOENCODING": "utf-8",
            "NO_PROXY": "127.0.0.1,localhost",
        })
        return env

    def command(self):
        return [sys.executable, "-m", "mini_agent", "--db", str(self.db), "--trace"]

    def invoke(self, env, text):
        return subprocess.run(
            self.command(), cwd=ROOT, env=env, input=text, encoding="utf-8",
            capture_output=True, timeout=10,
        )

    def test_direct_script_imports_outside_project_without_site_packages(self):
        entry = ROOT / "mini_agent" / "__main__.py"
        finished = subprocess.run(
            [sys.executable, "-I", "-S", "-X", "utf8", str(entry), "--help"],
            cwd=self.db.parent, encoding="utf-8", capture_output=True, timeout=10,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("--trace", finished.stdout)

    def test_cli_roundtrip_and_session_switch(self):
        requests = []

        def local(path, body):
            requests.append((path, body))
            if body["messages"][-1]["role"] == "tool":
                return response("已记入待办")
            return write_todo()

        env = self.server(local)
        finished = self.invoke(env, "/new 读书\n帮我记一条待办\n/sessions\n/switch 1\n/exit\n")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("已创建会话 #2", finished.stdout)
        self.assertIn("todo 成功", finished.stdout)
        self.assertIn("已记入待办", finished.stdout)
        self.assertIn("已切换到会话 #1", finished.stdout)
        self.assertEqual(requests[0][0], "/v1/chat/completions")
        self.assertEqual(requests[0][1]["model"], "local-test")
        self.assertEqual(requests[1][1]["messages"][-1]["tool_call_id"], "offline-add")
        self.assertNotIn("thinking", requests[0][1])

    def test_process_kill_after_committed_tool_then_restart(self):
        received_result = threading.Event()
        release_response = threading.Event()

        def local(_path, body):
            if body["messages"][-1]["role"] == "tool":
                received_result.set()
                release_response.wait(timeout=10)
                return response("答复尚未发出")
            return write_todo()

        env = self.server(local)
        worker = subprocess.Popen(
            self.command(), cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        try:
            worker.stdin.write("记一条待办\n")
            worker.stdin.flush()
            self.assertTrue(received_result.wait(timeout=8), "工具结果未到达本机模型替身")
            worker.kill()
            worker.communicate(timeout=5)
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=5)
            release_response.set()

        restarted = self.invoke(env, "/exit\n")
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertIn("上次中断", restarted.stdout)
        self.assertIn("已新增待办 #1", restarted.stdout)
        self.assertNotIn("答复尚未发出", restarted.stdout)
        again = self.invoke(env, "/exit\n")
        self.assertNotIn("上次中断", again.stdout)

        from mini_agent.storage import Store

        store = Store(self.db)
        try:
            self.assertEqual(store.list_todos(1), [{"id": 1, "title": "测试待办", "done": False}])
            self.assertEqual(store.completed_turns(1, 0), [])
        finally:
            store.close()
