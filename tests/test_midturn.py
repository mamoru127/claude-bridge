"""作業中の Claude への途中の指示と、停止時の CLI の後片付け。"""

import json
import os
import socket
import subprocess
import threading
import time
import unittest
import urllib.parse

from claude_bridge import cli_process
from claude_bridge.server import BridgeServer

from tests.test_app_server_mcp import FakeAppServer
from tests.test_bridge import (
    BridgeHTTPTestCase,
    FakeRunner,
    HangingProcess,
    HangingRunner,
    make_config,
)


class SteerEndpointTest(BridgeHTTPTestCase):
    """作業中の Claude へ指示が届いたとき、作業を区切って Codex へ返す。"""

    def start_bridge(self, runner):
        server = BridgeServer(make_config(), runner=runner)
        server.app_server = FakeAppServer()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.daemon = True
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return server, f"http://{host}:{port}"

    def run_claude(self, base_url, runner, stream=True):
        """作業中の Claude CLI を thread-1 に 1 本作る。応答は別スレッドで受け取る。"""

        results = []
        payload = {"input": "1問目", "prompt_cache_key": "thread-1"}
        send = self.post_stream if stream else self.post
        thread = threading.Thread(
            target=lambda: results.append(send(base_url, payload)), daemon=True
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        for _ in range(500):
            if runner.processes:
                break
            time.sleep(0.01)
        return thread, results

    def steered(self, base_url, thread_id):
        status, body = self.post(base_url, {"threadId": thread_id}, path="/app-server/steered")
        self.assertEqual(status, 200)
        return body["result"]["handedBack"]

    def test_working_cli_is_stopped_and_the_turn_closes_normally(self):
        runner = HangingRunner()
        _, base_url = self.start_bridge(runner)
        turn, results = self.run_claude(base_url, runner)

        self.assertTrue(self.steered(base_url, "thread-1"))
        turn.join(5)

        # 失敗ではなく完了で閉じる。Codex は差し込まれた指示を載せて次を送ってくる。
        names = [name for name, _ in results[0][1]]
        self.assertIn("response.completed", names)
        self.assertNotIn("response.failed", names)
        self.assertGreaterEqual(runner.processes[0].killed, 1)

    def test_non_stream_turn_also_closes_normally(self):
        runner = HangingRunner()
        _, base_url = self.start_bridge(runner)
        turn, results = self.run_claude(base_url, runner, stream=False)

        self.steered(base_url, "thread-1")
        turn.join(5)

        status, body = results[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")

    def test_other_threads_are_left_alone(self):
        runner = HangingRunner()
        _, base_url = self.start_bridge(runner)
        self.run_claude(base_url, runner)

        self.assertFalse(self.steered(base_url, "thread-2"))
        self.assertEqual(runner.processes[0].killed, 0)
        runner.processes[0].kill()

    def test_turn_start_through_the_bridge_hands_back_after_it_is_accepted(self):
        runner = HangingRunner()
        server, base_url = self.start_bridge(runner)
        turn, results = self.run_claude(base_url, runner)
        params = {"threadId": "thread-1", "input": [{"type": "text", "text": "英語で"}]}

        status, _ = self.post(
            base_url, {"method": "turn/start", "params": params}, path="/app-server/rpc"
        )
        turn.join(5)

        self.assertEqual(status, 200)
        self.assertEqual(server.app_server.started, [params])
        self.assertIn("response.completed", [name for name, _ in results[0][1]])


class ClientDisconnectTest(BridgeHTTPTestCase):
    def test_stop_kills_the_cli_while_it_is_silent(self):
        # 長い Bash を待つ間、CLI は何も出力しない。停止（切断）はその間に CLI を止める。
        runner = HangingRunner()
        base_url = self.start_server(make_config(timeout_seconds=60), runner)
        url = urllib.parse.urlsplit(base_url)
        body = json.dumps({"input": "やあ", "stream": True}).encode("utf-8")
        sock = socket.create_connection((url.hostname, url.port))
        sock.sendall(
            f"POST /v1/responses HTTP/1.1\r\nHost: {url.netloc}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        self.assertIn(b"200", sock.recv(4096))
        sock.close()

        for _ in range(300):
            if runner.processes and runner.processes[0].killed:
                break
            time.sleep(0.01)
        self.assertGreaterEqual(runner.processes[0].killed, 1)


class FirstHangsRunner(FakeRunner):
    """最初の 1 本だけ出力しないまま止まるランナー。以降は普通に答える。"""

    def make_process(self):
        return HangingProcess() if not self.processes else super().make_process()


class StoppedBeforeOutputTest(BridgeHTTPTestCase):
    def interrupt_first_run(self, base_url, runner, payload):
        self.post_and_disconnect(base_url, payload)
        for _ in range(500):
            if runner.processes and runner.processes[0].killed:
                return
            time.sleep(0.01)

    def test_new_conversation_stopped_before_output_starts_over(self):
        # 起動直後に止められた CLI は転記を作っていないことがある。続きにすると
        # 「No conversation found」で失敗し続けるため、全履歴で開始し直す。
        runner = FirstHangsRunner()
        base_url = self.start_server(make_config(), runner)
        self.interrupt_first_run(base_url, runner, {"input": "1問目", "prompt_cache_key": "t"})

        self.post(
            base_url,
            {
                "input": [
                    {"role": "user", "content": "1問目"},
                    {"role": "user", "content": "やっぱり英語で"},
                ],
                "prompt_cache_key": "t",
            },
        )

        command = runner.calls[1]["command"]
        self.assertNotIn("--resume", command)
        self.assertIn("1問目", runner.prompt(1))
        self.assertIn("やっぱり英語で", runner.prompt(1))

    def test_resumed_turn_stopped_before_output_is_passed_again(self):
        runner = FakeRunner()
        base_url = self.start_server(make_config(), runner)
        self.post(base_url, {"input": "1問目", "prompt_cache_key": "t"})
        runner.make_process = lambda: HangingProcess()
        history = [
            {"role": "user", "content": "1問目"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "2問目"},
        ]
        self.post_and_disconnect(base_url, {"input": history, "prompt_cache_key": "t"})
        for _ in range(500):
            if len(runner.processes) == 2 and runner.processes[1].killed:
                break
            time.sleep(0.01)

        del runner.make_process
        self.post(
            base_url,
            {
                "input": [*history, {"role": "user", "content": "やっぱり英語で"}],
                "prompt_cache_key": "t",
            },
        )

        # 止まった 2 問目は CLI に届いていない。続きとして 2 問目から渡し直す。
        self.assertIn("--resume", runner.calls[2]["command"])
        self.assertIn("2問目", runner.prompt(2))
        self.assertIn("やっぱり英語で", runner.prompt(2))


@unittest.skipIf(os.name == "nt", "子孫の洗い出しは ps を使う POSIX だけ")
class KillTreeTest(unittest.TestCase):
    def test_commands_started_by_the_cli_are_stopped_too(self):
        process = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(process.stdout.close)
        child = int(process.stdout.readline())

        cli_process.kill_tree(process)
        process.wait(5)

        for _ in range(200):
            alive = subprocess.run(
                ["ps", "-p", str(child), "-o", "stat="], capture_output=True, text=True
            ).stdout.strip()
            if not alive or alive.startswith("Z"):
                break
            time.sleep(0.01)
        self.assertTrue(not alive or alive.startswith("Z"))


if __name__ == "__main__":
    unittest.main()
