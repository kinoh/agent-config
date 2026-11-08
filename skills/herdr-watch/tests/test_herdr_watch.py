import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("herdr_watch", ROOT / "scripts/herdr_watch.py")
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

SESSION = {"agent": "claude", "kind": "id", "value": "fc2c2eda-ebae-47c3-9339-e5e5488c96a2"}
WORKER_SESSION = {"agent": "codex", "kind": "id", "value": "01a1531f-1434-7921-b523-b825dae9396a"}
FAKE_HERDR = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_HERDR_CALLS"], "a") as log:
        log.write(json.dumps(args) + "\\n")
    config = json.loads(open(os.environ["FAKE_HERDR_CONFIG"]).read())
    failure = config.get("fail", {{}}).get(" ".join(args[:2]))
    if failure:
        sys.stderr.write(json.dumps({{"error": {{"code": failure}}}}))
        sys.exit(1)
    if args[:2] == ["pane", "run"]:
        sys.exit(0)
    results = {{
        "pane current": {{"pane": {{"pane_id": os.environ["HERDR_PANE_ID"], "agent": config.get("caller_agent"),
                                    "agent_session": config["current_session"]}}}},
        "pane layout": {{"layout": {{"panes": [{{"pane_id": "w1:p1", "rect": {{"width": 200, "height": 50}}}}]}}}},
        "pane split": {{"pane": {{"pane_id": "w1:p2"}}}},
        "agent get": {{"agent": {{"pane_id": "w1:p1", "agent_session": config.get("session_at_notify")}}}},
    }}
    if args[:2] == ["agent", "get"] and args[2] == "worker":
        results["agent get"] = {{"agent": {{"pane_id": "w1:p3", "agent_status": config.get("worker_status"),
                                           "agent_session": None}}}}
    print(json.dumps({{"id": "fake", "result": results.get(" ".join(args[:2]), {{}})}}))
    """).format(python=sys.executable)


class HerdrWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        herdr = bin_dir / "herdr"
        herdr.write_text(FAKE_HERDR)
        herdr.chmod(0o755)
        self.calls = self.tmp / "calls.jsonl"
        self.config = self.tmp / "config.json"
        self.configure()
        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HERDR_ENV": "1",
               "HERDR_PANE_ID": "w1:p1", "FAKE_HERDR_CALLS": str(self.calls),
               "FAKE_HERDR_CONFIG": str(self.config)}
        patches = [mock.patch.dict(os.environ, env), mock.patch.object(watch, "STATE_DIR", self.tmp / "state")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def configure(self, caller_agent="codex", session_at_notify=SESSION, fail=None, worker_status="idle",
                  current_session=SESSION):
        self.config.write_text(json.dumps({"caller_agent": caller_agent, "session_at_notify": session_at_notify,
                                           "fail": fail or {}, "worker_status": worker_status,
                                           "current_session": current_session}))

    def reply_as_worker(self, run_dir, report, **config):
        self.configure(current_session=WORKER_SESSION, **config)
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p3"}):
            return watch.reply(run_dir, report)

    def herdr_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def events(self, run_dir):
        return [json.loads(line)["event"] for line in (run_dir / "events.jsonl").read_text().splitlines()]

    def started(self, command, timeout=None):
        started = watch.start("CI run", command, str(self.tmp), timeout)
        self.calls.unlink()
        return Path(started["run_dir"])

    def test_start_opens_an_unfocused_sibling_pane_that_runs_the_watcher(self):
        started = watch.start("CI run", ["gh", "run", "watch", "1"], str(self.tmp), None)
        run_dir = Path(started["run_dir"])
        self.assertEqual(started["watcher_pane"], "w1:p2")
        calls = self.herdr_calls()
        self.assertIn(["pane", "split", "--current", "--direction", "right", "--cwd", str(self.tmp), "--no-focus"], calls)
        run_call = next(c for c in calls if c[:2] == ["pane", "run"])
        self.assertEqual(run_call[2], "w1:p2")
        self.assertIn(f"run {run_dir}", run_call[3])
        request = json.loads((run_dir / "request.json").read_text())
        self.assertEqual(request["command"], ["gh", "run", "watch", "1"])
        self.assertEqual(request["caller_session"], SESSION)
        self.assertEqual(request["watcher_pane"], "w1:p2")
        self.assertNotIn(["agent", "prompt"], [c[:2] for c in calls])

    def test_start_requires_herdr_and_an_agent_in_the_calling_pane(self):
        with mock.patch.dict(os.environ, {"HERDR_ENV": "0"}):
            with self.assertRaisesRegex(watch.WatchError, "Herdr pane"):
                watch.start("CI run", ["true"], str(self.tmp), None)
        self.configure(caller_agent=None)
        with self.assertRaisesRegex(watch.WatchError, "no agent"):
            watch.start("CI run", ["true"], str(self.tmp), None)
        self.assertFalse(any(c[:2] == ["pane", "split"] for c in self.herdr_calls()))

    def test_finished_command_is_recorded_and_the_caller_is_prompted_once_ready(self):
        run_dir = self.started([sys.executable, "-c", "print('all green')"])
        out = io.BytesIO()
        self.assertEqual(watch.run(run_dir, out), 0)
        self.assertIn(b"all green", out.getvalue())
        self.assertIn("all green", (run_dir / "output.log").read_text())
        result = json.loads((run_dir / "result.json").read_text())
        self.assertEqual((result["status"], result["exit_code"]), ("succeeded", 0))
        calls = self.herdr_calls()
        wait_index = calls.index(["agent", "wait", "w1:p1", "--until", "idle", "--until", "done"])
        prompt_index = next(i for i, c in enumerate(calls) if c[:3] == ["agent", "prompt", "w1:p1"])
        self.assertLess(wait_index, prompt_index)
        message = calls[prompt_index][3]
        self.assertTrue(message.startswith("[herdr-watch:command 488c96a2] "))
        self.assertIn("CI run", message)
        self.assertIn("succeeded", message)
        self.assertIn(f"{run_dir}/result.json", message)
        self.assertIn("w1:p2", message)
        self.assertEqual(self.events(run_dir)[-1], "notified")

    def test_watcher_pane_shows_as_working_only_while_the_command_runs(self):
        run_dir = self.started([sys.executable, "-c", "print('all green')"])
        watch.run(run_dir, io.BytesIO())
        calls = self.herdr_calls()
        report = calls.index(["pane", "report-agent", "w1:p2", "--state", "working", "--message", "CI run",
                              "--source", "herdr-watch", "--agent", "herdr-watch"])
        release = calls.index(["pane", "release-agent", "w1:p2", "--source", "herdr-watch", "--agent", "herdr-watch"])
        wait = calls.index(["agent", "wait", "w1:p1", "--until", "idle", "--until", "done"])
        self.assertLess(report, release)
        self.assertLess(release, wait)
        events = self.events(run_dir)
        self.assertLess(events.index("display_working"), events.index("command_started"))
        self.assertLess(events.index("command_ended"), events.index("notified"))

    def test_display_failure_is_recorded_and_the_watch_continues(self):
        run_dir = self.started(["true"])
        self.configure(fail={"pane report-agent": "pane_not_found"})
        self.assertEqual(watch.run(run_dir, io.BytesIO()), 0)
        events = self.events(run_dir)
        self.assertIn("display_working_failed", events)
        self.assertEqual(events[-1], "notified")

    def test_failing_command_is_still_reported_with_its_exit_code(self):
        run_dir = self.started([sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertEqual(watch.run(run_dir, io.BytesIO()), 0)
        result = json.loads((run_dir / "result.json").read_text())
        self.assertEqual((result["status"], result["exit_code"]), ("failed", 3))
        prompt = next(c for c in self.herdr_calls() if c[:2] == ["agent", "prompt"])
        self.assertIn("failed（exit 3）", prompt[3])

    def test_timeout_terminates_the_command(self):
        run_dir = self.started([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        began = time.monotonic()
        self.assertEqual(watch.run(run_dir, io.BytesIO()), 0)
        self.assertLess(time.monotonic() - began, 15)
        self.assertEqual(json.loads((run_dir / "result.json").read_text())["status"], "timed_out")

    def test_replaced_caller_is_not_prompted(self):
        run_dir = self.started(["true"])
        self.configure(session_at_notify={"agent": "codex", "kind": "id", "value": "another-session"})
        self.assertEqual(watch.run(run_dir, io.BytesIO()), 1)
        self.assertFalse(any(c[:2] == ["agent", "prompt"] for c in self.herdr_calls()))
        self.assertEqual(self.events(run_dir)[-1], "notify_failed")

    def test_notification_failure_is_recorded(self):
        run_dir = self.started(["true"])
        self.configure(fail={"agent prompt": "agent_blocked"})
        self.assertEqual(watch.run(run_dir, io.BytesIO()), 1)
        last = json.loads((run_dir / "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual(last["event"], "notify_failed")
        self.assertIn("agent_blocked", last["error"])

    def delegated(self):
        run_dir = Path(watch.delegate("review", "worker", "Review the diff.")["run_dir"])
        self.calls.unlink()
        return run_dir

    def wait_for_event(self, run_dir, event):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if event in self.events(run_dir):
                return
            time.sleep(0.05)
        self.fail(f"{event} was not recorded: {self.events(run_dir)}")

    def test_delegate_sends_the_task_with_a_reply_instruction_without_waiting(self):
        started = watch.delegate("review", "worker", "Review the diff.")
        run_dir = Path(started["run_dir"])
        self.assertEqual(started["target_pane"], "w1:p3")
        prompts = [c for c in self.herdr_calls() if c[:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 1)
        target, text = prompts[0][2], prompts[0][3]
        self.assertEqual(target, "worker")
        self.assertTrue(text.startswith("[herdr-watch:delegate 488c96a2]\nReview the diff."))
        self.assertIn(f"reply {run_dir} --report {run_dir}/report.md", text)
        self.assertNotIn("--wait", prompts[0])
        self.assertFalse(any(c[:2] == ["agent", "wait"] for c in self.herdr_calls()))
        request = json.loads((run_dir / "request.json").read_text())
        self.assertEqual((request["kind"], request["caller_session"]), ("delegate", SESSION))

    def test_delegate_requires_a_ready_agent_other_than_the_caller(self):
        self.configure(worker_status="working")
        with self.assertRaisesRegex(watch.WatchError, "not ready"):
            watch.delegate("review", "worker", "Review the diff.")
        self.configure()
        with self.assertRaisesRegex(watch.WatchError, "itself"):
            watch.delegate("review", "w1:p1", "Review the diff.")
        self.assertFalse(any(c[:2] == ["agent", "prompt"] for c in self.herdr_calls()))

    def test_reply_returns_at_once_and_the_report_reaches_the_ready_caller(self):
        run_dir = self.delegated()
        report = self.tmp / "report.md"
        report.write_text("# done\n")
        self.assertEqual(self.reply_as_worker(run_dir, report)["report"], str(report))
        self.wait_for_event(run_dir, "notified")
        calls = self.herdr_calls()
        wait_index = calls.index(["agent", "wait", "w1:p1", "--until", "idle", "--until", "done"])
        prompt_index = next(i for i, c in enumerate(calls) if c[:3] == ["agent", "prompt", "w1:p1"])
        self.assertLess(wait_index, prompt_index)
        message = calls[prompt_index][3]
        self.assertTrue(message.startswith("[herdr-watch:reply dae9396a] "))
        self.assertIn("review", message)
        self.assertIn(str(report), message)

    def test_reply_to_a_replaced_caller_is_recorded_and_not_sent(self):
        run_dir = self.delegated()
        report = self.tmp / "report.md"
        report.write_text("# done\n")
        self.reply_as_worker(run_dir, report, session_at_notify={"value": "another-session"})
        self.wait_for_event(run_dir, "notify_failed")
        self.assertFalse(any(c[:2] == ["agent", "prompt"] for c in self.herdr_calls()))

    def test_reply_requires_an_existing_report_for_a_delegation(self):
        run_dir = self.delegated()
        with self.assertRaisesRegex(watch.WatchError, "not found"):
            watch.reply(run_dir, self.tmp / "missing.md")
        command_run = self.started(["true"])
        report = self.tmp / "report.md"
        report.write_text("# done\n")
        with self.assertRaisesRegex(watch.WatchError, "not a delegation"):
            watch.reply(command_run, report)

    def test_a_sender_without_a_session_id_sends_nothing(self):
        self.configure(current_session=None)
        with self.assertRaisesRegex(watch.WatchError, "session ID"):
            watch.delegate("review", "worker", "Review the diff.")
        self.assertFalse(any(c[:2] == ["agent", "prompt"] for c in self.herdr_calls()))


if __name__ == "__main__":
    unittest.main()
