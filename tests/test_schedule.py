from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
import urllib.parse

from websockets.sync.server import serve

ROOT = Path(__file__).resolve().parents[1]
SCHEDULE = ROOT / "bin/launch-schedule"
TOKEN = "test-t3-secret-token"
FAKE_LAUNCH = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_ARGS"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "log_root": os.environ.get("LAUNCH_LOG_ROOT")}) + "\\n")
print(os.environ["FAKE_PICK"])
"""
TARGET = {"providerInstanceId": "codex", "model": "gpt-6.2-sol", "options": {"reasoningEffort": "medium"}}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "prompts").mkdir()
        (self.root / "prompts/daily.txt").write_text("Do the daily thing.\nLine two.")
        (self.root / "prompts/night.txt").write_text("Night run.")
        (self.root / "schedule.yaml").write_text(json.dumps({"jobs": [
            {"name": "daily", "time": "09:00", "app": "okilum", "thread": "thread-a", "prompt_file": "prompts/daily.txt"},
            {"name": "night", "time": "03:00", "app": "toroid", "thread": "thread-b", "fresh": True,
             "prompt_file": "prompts/night.txt", "tier": "standard"},
        ]}))
        launch = self.root / "launch"
        launch.write_text(FAKE_LAUNCH)
        launch.chmod(0o755)
        self.payloads, self.telegram, self.reject = [], [], False
        ws_port, tg_port = self._start_t3(), self._start_telegram()
        (self.root / "t3.env").write_text(f"T3_URL=http://127.0.0.1:{ws_port}\nT3_TOKEN={TOKEN}\n")
        (self.root / "tg.env").write_text("TG_TOKEN=tg-secret\nTG_CHAT=42\n")
        (self.root / "receipts.jsonl").write_text('{"stage": "launched"}\n')
        self.env = dict(os.environ, LAUNCH_SCHEDULE=str(self.root / "schedule.yaml"), LAUNCH_BIN=str(launch),
                        LAUNCH_SCHEDULE_STATE=str(self.root / "state.json"), LAUNCH_T3_ENV=str(self.root / "t3.env"),
                        LAUNCH_TG_ENV=str(self.root / "tg.env"), LAUNCH_TG_API=f"http://127.0.0.1:{tg_port}",
                        LAUNCH_NOTIFY_THREAD="ga-thread", LAUNCH_TIMEZONE="Europe/Helsinki", LAUNCH_LOG_ROOT=str(self.root),
                        FAKE_ARGS=str(self.root / "args.jsonl"), FAKE_PICK=json.dumps({"pool": "codex", "target": TARGET, "why": "codex: 60% remaining"}))

    def _start_t3(self):
        def handler(ws):
            if ws.request.headers.get("Authorization") != f"Bearer {TOKEN}":
                ws.close()
                return
            frame = json.loads(ws.recv())
            self.payloads.append(frame["payload"])
            ws.send(json.dumps({"_tag": "Ping"}))
            outcome = {"_tag": "Failure"} if self.reject else {"_tag": "Success", "value": {"sequence": len(self.payloads)}}
            ws.send(json.dumps([{"_tag": "Exit", "requestId": frame["id"], "exit": outcome}]))
            ws.recv()  # Pong

        server = serve(handler, "127.0.0.1", 0)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server.socket.getsockname()[1]

    def _start_telegram(self):
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                test.telegram.append((self.path, urllib.parse.parse_qs(body)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def run_at(self, iso, *extra, status=0):
        result = subprocess.run([SCHEDULE, "--now", iso, *extra], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, status, result.stderr)
        return result

    def picks(self):
        path = self.root / "args.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_due_job_dispatches_with_fresh_pick_once_per_slot(self):
        self.run_at("2026-11-26T09:01:00+02:00")
        self.assertEqual(len(self.payloads), 1)
        payload = self.payloads[0]
        self.assertEqual(payload["type"], "message.dispatch")
        self.assertEqual(payload["threadId"], "thread-a")
        self.assertEqual(payload["text"], "Do the daily thing.\nLine two.")
        self.assertEqual(payload["modelSelection"], {"instanceId": "codex", "model": "gpt-6.2-sol",
                                                     "options": [{"id": "reasoningEffort", "value": "medium"}]})
        self.assertEqual(payload["dispatchMode"], {"type": "queue_after_active"})
        self.assertNotIn("freshContext", payload)
        self.assertEqual(self.picks()[0]["argv"][:5], ["pick", "--app", "okilum", "--change", "daily-2026-11-26"])
        self.run_at("2026-11-26T09:02:00+02:00")
        self.run_at("2026-11-26T10:30:00+02:00")
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(len(self.picks()), 1)
        state = json.loads((self.root / "state.json").read_text())
        self.assertEqual(state["daily"]["status"], "sent")
        self.assertEqual(self.telegram, [])

    def test_fresh_job_sets_fresh_context_and_tier(self):
        self.run_at("2026-11-26T03:00:30+02:00")
        self.assertTrue(self.payloads[0]["freshContext"])
        self.assertIn("standard", self.picks()[0]["argv"])

    def test_outside_grace_window_does_nothing(self):
        self.run_at("2026-11-26T08:59:00+02:00")
        self.run_at("2026-11-26T11:01:00+02:00")
        self.assertEqual(self.payloads, [])
        self.assertEqual(self.picks(), [])

    def test_deny_notifies_oleg_and_ga_thread_once(self):
        self.env["FAKE_PICK"] = json.dumps({"deny": "quota", "why": "codex: exhausted"})
        self.run_at("2026-11-26T09:00:10+02:00")
        self.run_at("2026-11-26T09:01:10+02:00")
        self.assertEqual(len(self.telegram), 1)
        path, form = self.telegram[0]
        self.assertEqual(path, "/bottg-secret/sendMessage")
        self.assertEqual(form["chat_id"], ["42"])
        self.assertIn("codex: exhausted", form["text"][0])
        self.assertEqual([p["threadId"] for p in self.payloads], ["ga-thread"])
        self.assertNotIn("modelSelection", self.payloads[0])
        self.assertEqual(json.loads((self.root / "state.json").read_text())["daily"]["status"], "denied")

    def test_rejected_dispatch_retries_with_same_command_and_target(self):
        self.reject = True
        self.run_at("2026-11-26T09:00:00+02:00")
        self.reject = False
        self.run_at("2026-11-26T09:01:00+02:00")
        job = [p for p in self.payloads if p["threadId"] == "thread-a"]
        self.assertEqual(len(job), 2)
        self.assertEqual(job[0]["commandId"], job[1]["commandId"])
        self.assertEqual(len(self.picks()), 1)
        self.assertEqual(len(self.telegram), 1)
        self.assertEqual(json.loads((self.root / "state.json").read_text())["daily"]["status"], "sent")

    def test_dry_run_prints_picks_without_touching_state_or_receipts(self):
        out = self.run_at("2026-11-25T12:00:00+02:00", "--dry-run").stdout.splitlines()
        rows = [json.loads(line) for line in out]
        self.assertEqual([r["job"] for r in rows], ["night", "daily"])
        self.assertEqual(rows[0]["date"], "2026-11-26")
        self.assertEqual(rows[1]["target"], TARGET)
        self.assertEqual(self.payloads, [])
        self.assertFalse((self.root / "state.json").exists())
        self.assertTrue(all(p["log_root"] != str(self.root) for p in self.picks()))
        self.assertEqual((self.root / "receipts.jsonl").read_text(), '{"stage": "launched"}\n')

    def test_missing_prompt_file_fails(self):
        (self.root / "prompts/night.txt").unlink()
        self.assertIn("launch-schedule:", self.run_at("2026-11-26T09:00:00+02:00", status=1).stderr)


if __name__ == "__main__":
    unittest.main()
