from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import unittest


LAUNCH = Path(__file__).resolve().parents[1] / "bin/launch"
POLICY = {
    "worker_pool": {
        "work": {"feature": ["strong", "standard"], "rebase": ["strong", "standard", "mechanical"]},
        "models": [
            {"tier": "strong", "instance": "claudeAgent", "model": "claude-opus-5-5", "effort": "medium"},
            {"tier": "strong", "instance": "cursor", "model": "grok-4.7"},
            {"tier": "strong", "instance": "codex", "model": "gpt-6.2-sol", "reasoningEffort": "low"},
            {"tier": "strong", "instance": "antigravity", "model": "gemini-4-neon", "thought_level": "medium"},
            {"tier": "strong", "instance": "antigravity", "model": "claude-sonnet-5-5", "status": "route-unverified"},
            {"tier": "standard", "instance": "codex", "model": "ollama/kimi-k3"},
            {"tier": "mechanical", "instance": "opencode", "model": "opencode/mimo-free", "group": "opencode-free"},
        ],
    },
}


class LaunchTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.snapshot = self.root / "snapshot.json"
        self.feed = self.root / "quotas-live.json"
        self.policy = self.root / "policy.yaml"
        self.t3_env = self.root / "api-token.env"
        self.policy.write_text(json.dumps(POLICY))
        self.t3_threads = {}
        self.t3_requests = []
        self._start_t3_mock()
        self.env = dict(
            os.environ,
            LAUNCH_LOG_ROOT=str(self.root),
            LAUNCH_SNAPSHOT=str(self.snapshot),
            LAUNCH_FEED=str(self.feed),
            LAUNCH_POLICY=str(self.policy),
            LAUNCH_T3_ENV=str(self.t3_env),
        )
        self.now = datetime.now(timezone.utc)

    def _start_t3_mock(self):
        test = self
        token = "test-t3-secret-token"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                test.t3_requests.append((self.path, dict(self.headers)))
                if self.headers.get("Authorization") != f"Bearer {token}":
                    self.send_response(401)
                    self.end_headers()
                    return
                if self.headers.get("x-t3-orchestration-protocol") != "2":
                    self.send_response(400)
                    self.end_headers()
                    return
                prefix, suffix = "/api/orchestration/threads/", "/bounded"
                if self.path.startswith(prefix) and self.path.endswith(suffix):
                    tid = self.path[len(prefix):-len(suffix)]
                    if tid in test.t3_threads:
                        body = json.dumps(test.t3_threads[tid]).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.t3_env.write_text(f"T3_URL=http://127.0.0.1:{server.server_port}\nT3_TOKEN={token}\n")

    def mock_t3_thread(self, thread_id="child", run_id="run-1", model="gpt-6.2-sol", minutes=7):
        end = self.now
        start = end - timedelta(minutes=minutes)
        self.t3_threads[thread_id] = {
            "projection": {
                "thread": {"id": thread_id},
                "runs": [{
                    "id": run_id, "ordinal": 1, "status": "completed",
                    "startedAt": start.isoformat(), "completedAt": end.isoformat(),
                    "modelSelection": {"model": model},
                }],
            }
        }

    def write_snapshot(self, age=0, **sections):
        generated = (self.now - timedelta(seconds=age)).isoformat()
        self.snapshot.write_text(json.dumps({"generated": generated, **sections}))

    def write_feed(self, age=0, **providers):
        generated = (self.now - timedelta(seconds=age)).isoformat()
        self.feed.write_text(json.dumps({"generated_at": generated, "providers": providers}))

    def quota(self, remaining, status=None, age=0, reset_minutes=120, **fields):
        return {"remaining_fraction": remaining, "status": status or ("ok" if remaining else "exhausted"),
                "observed_at": (self.now - timedelta(seconds=age)).isoformat(),
                "reset_at": (self.now + timedelta(minutes=reset_minutes)).isoformat(), **fields}

    def claude_account(self, weekly_used, reset_hours=120):
        reset_at = (self.now + timedelta(hours=reset_hours)).isoformat()
        return {"ok": True, "fetched_at": self.now.isoformat(),
                "data": {"seven_day": {"utilization": weekly_used, "resets_at": reset_at}}}

    def set_models(self, models):
        policy = json.loads(self.policy.read_text())
        policy["worker_pool"]["models"] = models
        self.policy.write_text(json.dumps(policy))

    def run_cli(self, *argv, status=0):
        result = subprocess.run([LAUNCH, *argv], env=self.env, capture_output=True, text=True)
        if status == 0:
            self.assertEqual(result.stderr, "")
        self.assertEqual(result.returncode, status, result.stderr)
        return json.loads(result.stdout) if result.stdout else result.stderr

    def pick(self, *args, app="app", change="change", role="implementation", tier="strong"):
        return self.run_cli("pick", "--app", app, "--change", change, "--role", role, "--tier", tier, *args)

    def receipts(self):
        return [json.loads(line) for line in (self.root / "receipts.jsonl").read_text().splitlines()]

    def test_instance_config_fills_missing_settings_without_overriding_environment(self):
        config_root = self.root / "config"
        config_root.joinpath("launch").mkdir(parents=True)
        config_root.joinpath("launch", "instance.env").write_text(
            f"LAUNCH_POLICY={self.policy}\nLAUNCH_LOG_ROOT={self.root / 'unused'}\n"
        )
        self.env["XDG_CONFIG_HOME"] = str(config_root)
        self.env.pop("LAUNCH_POLICY")
        self.assertIn("target", self.pick())
        self.assertEqual(len(self.receipts()), 1)
        self.assertFalse((self.root / "unused").exists())
        self.policy.unlink()
        error = self.run_cli("pick", "--app", "sample-app", "--change", "missing-policy", status=1)
        self.assertIn("Set LAUNCH_POLICY in launch/instance.env or the environment", error)

    def test_model_choice_and_one_receipt_line_per_launch(self):
        first = self.pick(app="okilum", change="c-1")
        self.assertEqual(first["pool"], "cursor")
        self.assertEqual(
            first["target"],
            {"providerInstanceId": "cursor", "model": "grok-4.7", "options": {"reasoning_effort": "medium"}},
        )
        second = self.pick(app="okilum", change="c-2", role="mechanical", tier="mechanical")
        self.assertEqual(second["target"]["providerInstanceId"], "cursor")
        rows = self.receipts()
        self.assertEqual(len(rows), 2)
        self.assertEqual([(r["project"], r["change"], r["stage"]) for r in rows],
                         [("okilum", "c-1", "launched"), ("okilum", "c-2", "launched")])

    def test_skips_unenabled_rows_and_respects_tier_and_role(self):
        self.set_models([
            {"tier": "strong", "instance": "antigravity", "model": "claude-sonnet-5-5", "status": "route-unverified"},
            {"tier": "mechanical", "instance": "opencode", "model": "opencode/mimo-free", "group": "opencode-free"},
        ])
        denied = self.pick(change="feat", role="implementation", tier="mechanical")
        self.assertEqual(denied["deny"], "quota")
        mech = self.pick(change="reb", role="mechanical", tier="mechanical")
        self.assertEqual(mech["pool"], "opencode-free")
        self.assertEqual(mech["target"], {"providerInstanceId": "opencode", "model": "opencode/mimo-free", "options": {}})

    def test_legacy_pick_sizes_and_installed_receipt(self):
        sha = "a" * 40
        evidence = f"Sprint ready: main {sha} · /Applications/SampleApp.app"
        # Installation recording does not need a model policy or a T3 connection.
        policy = self.policy.read_text()
        self.policy.unlink()
        self.assertEqual(self.run_cli("receipt", "installed", "--app", "okilum", "--change", sha,
                                      "--by", "m4tick", "--evidence", evidence), "")
        self.policy.write_text(policy)
        m4 = self.pick("--size", "S", app="okilum", change=sha, role="mechanical", tier="mechanical")
        watcher = self.pick("--size", "M", tier="standard")
        installed, *launches = self.receipts()
        self.assertIsNotNone(datetime.fromisoformat(installed.pop("at")).tzinfo)
        self.assertEqual(installed, {"project": "okilum", "stage": "installed", "change": sha,
                                     "by": "m4tick", "evidence": [evidence]})
        self.assertEqual([r["launch_id"] for r in launches], [m4["launch_id"], watcher["launch_id"]])
        self.assertEqual(m4["target"], watcher["target"])

    def test_row_effort_thought_level_and_risk_high_option_ids(self):
        cases = [
            ("codex", "gpt-6.2-sol", {"reasoningEffort": "low"}, "reasoningEffort", "low"),
            ("cursor", "grok-4.7", {"effort": "low"}, "reasoning_effort", "low"),
            ("antigravity", "gemini-4-neon", {"thought_level": "medium"}, "thought_level", "medium"),
        ]
        for inst, model, row_extra, opt_key, expected_normal in cases:
            with self.subTest(provider=inst):
                self.set_models([{"tier": "strong", "instance": inst, "model": model, **row_extra}])
                normal = self.pick(change=f"{inst}-norm")
                self.assertEqual(normal["target"], {"providerInstanceId": inst, "model": model, "options": {opt_key: expected_normal}})
                high = self.pick("--risk", "high", change=f"{inst}-high")
                self.assertEqual(high["target"], {"providerInstanceId": inst, "model": model, "options": {opt_key: "high"}})

    def test_claude_15_percent_cap_boundaries(self):
        self.write_snapshot(
            proxy_auths=[
                {"provider": "claude", "email": "primary@example.test", "status": "active"},
                {"provider": "claude", "email": "empty@example.test", "status": "active"},
            ],
            claude_usage={
                "primary@example.test": self.claude_account(33.0),
                "empty@example.test": self.claude_account(100.0),
            },
        )
        # Normal risk never picks native Claude.
        for i in range(5):
            self.assertNotEqual(self.pick(change=f"seed-{i}")["pool"], "claude")
        # 6th launch (1/6 = 16.7% > 15%) is still capped.
        sixth = self.pick("--risk", "high", change="sixth")
        self.assertNotEqual(sixth["pool"], "claude")
        self.assertIn("claude: weekly 15% cap", sixth["why"])
        # 7th launch (1/7 = 14.3% <= 15%) is allowed.
        seventh = self.pick("--risk", "high", change="seventh")
        self.assertEqual(seventh["pool"], "claude")
        self.assertEqual(
            seventh["target"],
            {"providerInstanceId": "claudeAgent", "model": "claude-opus-5-5", "options": {"effort": "high"}},
        )
        # 8th launch (2/8 = 25% > 15%) is capped again.
        eighth = self.pick("--risk", "high", change="eighth")
        self.assertNotEqual(eighth["pool"], "claude")
        self.assertIn("claude: weekly 15% cap", eighth["why"])

    def test_claude_weekly_50_percent_boundaries_and_auth_filtering(self):
        for i in range(6):
            self.pick(change=f"seed-{i}")
        # Missing snapshot -> weekly unknown.
        missing = self.pick("--risk", "high", change="missing-snap")
        self.assertNotEqual(missing["pool"], "claude")
        self.assertIn("claude: weekly unknown", missing["why"])

        # Exactly 50.0% remaining (50.0% used) -> denied (must be > 50%).
        self.write_snapshot(claude_usage={"acct": self.claude_account(50.0)})
        at_50 = self.pick("--risk", "high", change="at-50")
        self.assertNotEqual(at_50["pool"], "claude")
        self.assertIn("claude: weekly <= 50%", at_50["why"])

        # Disabled account with 90% left is ignored; active hashed account with 51% left allows Claude.
        active_email = "active@example.test"
        hashed = sha256(f"oauth-account:claude:{active_email}".encode()).hexdigest()[:24]
        self.write_snapshot(
            proxy_auths=[
                {"provider": "claude", "email": active_email, "status": "active"},
                {"provider": "claude", "email": "disabled@example.test", "disabled": True},
            ],
            claude_usage={
                hashed: self.claude_account(49.0),
                "disabled@example.test": self.claude_account(10.0),
            },
        )
        above_50 = self.pick("--risk", "high", change="above-50")
        self.assertEqual(above_50["pool"], "claude")

    def test_fresh_feed_ranks_allowed_models_and_keeps_policy_order_on_ties(self):
        self.write_feed(cursor=self.quota(0), codex=self.quota(0.37),
                        antigravity=self.quota(0.9, groups={
                            "gemini": self.quota(0.41), "claude_gpt": self.quota(0.9),
                        }),
                        opencode=self.quota(1))
        result = self.pick(tier="standard")
        self.assertEqual(result["target"]["model"], "gemini-4-neon")
        self.assertIn("cursor: exhausted", result["why"])
        self.assertIn("41% remaining", result["why"])
        self.write_feed(cursor=self.quota(0.41), codex=self.quota(0.41))
        self.assertEqual(self.pick(tier="standard")["pool"], "cursor")

    def test_maps_provider_aliases_and_antigravity_group(self):
        self.set_models([
            {"tier": "strong", "instance": "grok", "model": "grok-4.7"},
            {"tier": "strong", "instance": "muse", "model": "muse-wonder-1.0"},
            {"tier": "strong", "instance": "antigravity", "model": "gemini-4-neon"},
            {"tier": "standard", "instance": "opencode", "group": "ag-claude-gpt", "model": "cliproxy/gpt-oss-120b-medium"},
        ])
        self.write_feed(xai=self.quota(0), meta=self.quota(0.45), opencode=self.quota(0.8),
                        antigravity=self.quota(0.54, groups={
                            "gm": self.quota(0.9), "gemini": self.quota(0), "claude_gpt": self.quota(0.54),
                        }))
        self.assertEqual(self.pick(tier="strong")["pool"], "muse")
        self.assertEqual(self.pick(tier="standard")["pool"], "ag-claude-gpt")

    def test_codex_promo_credits_do_not_cover_ollama_or_a_cooldown(self):
        credits = {"has_credits": True, "overage_limit_reached": False}
        account = {"ok": True, "fetched_at": self.now.isoformat(), "data": {"credits": credits}}
        self.write_snapshot(codex_usage={"promo": account})
        self.write_feed(cursor=self.quota(0), codex=self.quota(0), ollama=self.quota(0))
        result = self.pick(tier="standard")
        self.assertEqual(result["target"]["model"], "gpt-6.2-sol")
        self.assertIn("promo credits available", result["why"])
        self.write_feed(codex=self.quota(0, "cooldown"), ollama=self.quota(0))
        self.assertEqual(self.pick(tier="standard")["deny"], "quota")
        self.write_feed(codex=self.quota(0), ollama=self.quota(0))
        credits["overage_limit_reached"] = True
        self.write_snapshot(codex_usage={"promo": account})
        self.assertEqual(self.pick(tier="standard")["deny"], "quota")

    def test_missing_and_stale_feed_use_policy_order(self):
        missing = self.pick()
        self.assertEqual(missing["pool"], "cursor")
        self.assertIn("snapshot unavailable", missing["why"])
        self.write_feed(age=30 * 60 + 1, cursor=self.quota(0), codex=self.quota(0.7))
        stale = self.pick()
        self.assertEqual(stale["pool"], "cursor")
        self.assertIn("snapshot stale", stale["why"])

    def test_fresh_feed_with_unverified_or_expired_rows_denies(self):
        self.write_feed(cursor=self.quota(None, "unknown"), codex=self.quota(0.9, age=601, max_age_seconds=600),
                        antigravity=self.quota(0.5, groups={"gemini": self.quota(0.5, reset_minutes=-5)}))
        result = self.pick()
        self.assertEqual(result["deny"], "quota")
        self.assertIn("cursor: quota unknown", result["why"])
        self.assertIn("codex: quota unknown", result["why"])

    def test_claude_requires_short_window_on_account_that_passes_weekly_gate(self):
        for i in range(6):
            self.pick(change=f"seed-{i}")
        weekly_ok = self.claude_account(40.0)
        weekly_ok["data"]["five_hour"] = {"utilization": 100.0}
        weekly_low = self.claude_account(60.0)
        self.write_snapshot(claude_usage={"weekly-ok": weekly_ok, "weekly-low": weekly_low})
        self.write_feed(cursor=self.quota(0.2))
        result = self.pick("--risk", "high")
        self.assertEqual(result["pool"], "cursor")
        self.assertIn("claude: exhausted", result["why"])

    def test_t3_bounded_api_path_via_done(self):
        self.mock_t3_thread("child-1", "run-9", "gpt-6.2-sol", minutes=7)
        run = self.run_cli("done", "--thread", "child-1")
        self.assertEqual(len(self.t3_requests), 1)
        self.assertEqual(self.t3_requests[0][0], "/api/orchestration/threads/child-1/bounded")
        self.assertEqual((run["run_id"], run["model"], run["agent_min"]), ("run-9", "gpt-6.2-sol", 7))


if __name__ == "__main__":
    unittest.main()
