#!/usr/bin/env python3
"""Let a Herdr agent end its turn instead of waiting, and wake it with a message later.

``start`` runs in the agent's pane: it opens the watcher pane and returns at once.
``run`` runs in the watcher pane: it executes the command and records the result.
``delegate`` sends a task to another agent without waiting; the task ends with an
instruction to call ``reply``, which returns at once and leaves ``notify-reply``
running in the background.  Both paths wait until the calling agent is ready for
input and submit one prompt that points to the recorded result.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO


STATE_DIR = Path.home() / ".local" / "state" / "herdr-watch"
SCRIPT = Path(__file__).resolve()
READY_STATES = ("idle", "done")
# Agent label of the watcher pane while its command runs, so the space shows it as working.
DISPLAY_AGENT = "herdr-watch"
TERMINATE_GRACE_SECONDS = 10


class WatchError(RuntimeError):
    """A failure the caller must see; nothing is retried silently."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _herdr(*args: str, prints_json: bool = True) -> dict[str, Any]:
    operation = " ".join(args[:2])
    try:
        completed = subprocess.run(["herdr", *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise WatchError(f"herdr {operation} could not start") from exc
    if completed.returncode:
        code = "command_failed"
        try:
            error = json.loads(completed.stderr).get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                code = error["code"]
        except (json.JSONDecodeError, AttributeError):
            pass
        raise WatchError(f"herdr {operation} failed (code={code}, exit={completed.returncode})")
    if not prints_json:
        return {}
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WatchError(f"herdr {operation} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise WatchError(f"herdr {operation} returned invalid JSON")
    return value.get("result", value)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def _record(run_dir: Path, event: str, **fields: Any) -> None:
    with (run_dir / "events.jsonl").open("a") as log:
        log.write(json.dumps({"at": _now(), "event": event, **fields}, ensure_ascii=False) + "\n")


def _choose_direction(layout: dict[str, Any], pane_id: str) -> str:
    panes = (layout.get("layout") or {}).get("panes")
    pane = next((p for p in panes or [] if isinstance(p, dict) and p.get("pane_id") == pane_id), None)
    rect = pane.get("rect") if pane else None
    if not isinstance(rect, dict) or not isinstance(rect.get("width"), (int, float)) \
            or not isinstance(rect.get("height"), (int, float)):
        raise WatchError("unknown Herdr pane geometry")
    return "right" if rect["width"] >= rect["height"] else "down"


def _run_id(label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-")[:40] or "watch"
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{slug}-{uuid.uuid4().hex[:6]}"


def _caller() -> dict[str, Any]:
    caller_pane = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("HERDR_ENV") != "1" or not caller_pane:
        raise WatchError("herdr-watch requires a Herdr pane (HERDR_ENV=1)")
    current = _herdr("pane", "current", "--current").get("pane") or {}
    if current.get("pane_id") != caller_pane:
        raise WatchError("HERDR_PANE_ID does not match the current Herdr pane")
    if not current.get("agent"):
        raise WatchError("no agent is running in the calling pane")
    return {"caller_pane": caller_pane, "caller_agent": current["agent"],
            "caller_session": current.get("agent_session")}


def _tag(kind: str, sender_session: dict[str, Any] | None) -> str:
    """Mark messages that no human typed, so transcripts can be aggregated by kind and sender.

    The tail is used because Codex session IDs start with their creation time.
    """
    value = (sender_session or {}).get("value")
    if not isinstance(value, str) or len(value) < 8:
        raise WatchError("the sending agent has no Herdr session ID")
    return f"[herdr-watch:{kind} {value[-8:]}]"


def _create_run(label: str, request: dict[str, Any]) -> Path:
    run_dir = STATE_DIR / _run_id(label)
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "request.json", request)
    _record(run_dir, "created", kind=request["kind"], caller_pane=request["caller_pane"])
    return run_dir


def start(label: str, command: list[str], cwd: str, timeout: int | None) -> dict[str, Any]:
    if not command:
        raise WatchError("no command to watch")
    if timeout is not None and timeout <= 0:
        raise WatchError("timeout must be a positive number of seconds")
    caller = _caller()
    caller_pane = caller["caller_pane"]
    request = {"kind": "command", "label": label, "command": command, "cwd": cwd,
               "timeout_seconds": timeout, **caller, "created_at": _now()}
    run_dir = _create_run(label, request)
    try:
        direction = _choose_direction(_herdr("pane", "layout", "--current"), caller_pane)
        pane_id = (_herdr("pane", "split", "--current", "--direction", direction,
                          "--cwd", cwd, "--no-focus").get("pane") or {}).get("pane_id")
        if not isinstance(pane_id, str):
            raise WatchError("Herdr did not return the watcher pane")
        request["watcher_pane"] = pane_id
        _write_json(run_dir / "request.json", request)
        _record(run_dir, "pane_created", watcher_pane=pane_id)
        _herdr("pane", "rename", pane_id, f"watch: {label}")
        # `pane run` prints nothing on success.
        _herdr("pane", "run", pane_id, shlex.join([sys.executable, str(SCRIPT), "run", str(run_dir)]),
               prints_json=False)
        _record(run_dir, "watcher_started", watcher_pane=pane_id)
    except WatchError as exc:
        _record(run_dir, "start_failed", error=str(exc))
        raise
    return {"run_dir": str(run_dir), "watcher_pane": pane_id, "caller_pane": caller_pane}


def _execute(request: dict[str, Any], log_path: Path, out: BinaryIO) -> tuple[str, int | None]:
    proc = subprocess.Popen(request["command"], cwd=request["cwd"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    expired = threading.Event()

    def terminate() -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def expire() -> None:
        expired.set()
        terminate()

    timer = threading.Timer(request["timeout_seconds"], expire) if request["timeout_seconds"] else None
    if timer:
        timer.start()
    try:
        with log_path.open("wb") as log:
            for chunk in iter(lambda: proc.stdout.read1(65536), b""):
                log.write(chunk)
                log.flush()
                out.write(chunk)
                out.flush()
        code = proc.wait()
    except KeyboardInterrupt:
        terminate()
        return "interrupted", proc.wait()
    finally:
        if timer:
            timer.cancel()
        proc.stdout.close()
    if expired.is_set():
        return "timed_out", code
    return ("succeeded" if code == 0 else "failed"), code


def _notify(run_dir: Path, request: dict[str, Any], message: str) -> None:
    caller = request["caller_pane"]
    _record(run_dir, "waiting_for_caller", caller_pane=caller)
    _herdr("agent", "wait", caller, *[arg for state in READY_STATES for arg in ("--until", state)])
    agent = _herdr("agent", "get", caller).get("agent") or {}
    if agent.get("agent_session") != request["caller_session"]:
        raise WatchError("the calling agent was replaced; the message was not sent")
    _herdr("agent", "prompt", caller, message)


def _notify_and_record(run_dir: Path, request: dict[str, Any], message: str) -> int:
    try:
        _notify(run_dir, request, message)
    except WatchError as exc:
        _record(run_dir, "notify_failed", error=str(exc))
        print(f"[herdr-watch] notify failed: {exc}", file=sys.stderr, flush=True)
        return 1
    _record(run_dir, "notified", caller_pane=request["caller_pane"])
    print("[herdr-watch] notified", flush=True)
    return 0


def _report_display(run_dir: Path, event: str, *args: str) -> None:
    """Show the watcher pane as a working agent; the watch itself does not depend on it."""
    try:
        _herdr("pane", *args, "--source", DISPLAY_AGENT, "--agent", DISPLAY_AGENT, prints_json=False)
    except WatchError as exc:
        _record(run_dir, f"{event}_failed", error=str(exc))
        print(f"[herdr-watch] {event} failed: {exc}", file=sys.stderr, flush=True)
        return
    _record(run_dir, event)


def run(run_dir: Path, out: BinaryIO) -> int:
    request = json.loads((run_dir / "request.json").read_text())
    pane = request["watcher_pane"]
    _report_display(run_dir, "display_working", "report-agent", pane, "--state", "working",
                    "--message", request["label"])
    _record(run_dir, "command_started", command=request["command"])
    started = time.monotonic()
    started_at = _now()
    try:
        status, code = _execute(request, run_dir / "output.log", out)
    except OSError as exc:
        status, code = "failed_to_start", None
        _record(run_dir, "command_failed_to_start", error=str(exc))
    finally:
        _report_display(run_dir, "display_released", "release-agent", pane)
    result = {"status": status, "exit_code": code, "started_at": started_at, "ended_at": _now(),
              "duration_seconds": round(time.monotonic() - started)}
    _write_json(run_dir / "result.json", result)
    _record(run_dir, "command_ended", status=status, exit_code=code)
    print(f"\n[herdr-watch] {status} (exit {code}); notifying {request['caller_pane']}", flush=True)
    message = (f"{_tag('command', request['caller_session'])} 「{request['label']}」が終了した。"
               f"状態: {status}（exit {code}）、所要 {result['duration_seconds']}秒。"
               f"結果: {run_dir}/result.json 出力: {run_dir}/output.log "
               f"確認したら監視ペイン {request['watcher_pane']} を閉じて。")
    return _notify_and_record(run_dir, request, message)


def _reply_instruction(run_dir: Path) -> str:
    report = run_dir / "report.md"
    command = shlex.join([sys.executable, str(SCRIPT), "reply", str(run_dir), "--report", str(report)])
    return ("\n\n---\n"
            "この依頼の結果は、次の方法で依頼元へ返す。依頼元は待たずにターンを終えている。\n"
            f"- 作業を終えたら、報告を Markdown で {report} に書き、最後に次を実行してからターンを終える。\n"
            "- 判断が必要で先へ進めない場合も、質問を報告に書いて同じように返す。\n"
            f"{command}")


def delegate(label: str, target: str, prompt: str) -> dict[str, Any]:
    if not prompt.strip():
        raise WatchError("no task to delegate")
    caller = _caller()
    tag = _tag("delegate", caller["caller_session"])
    agent = _herdr("agent", "get", target).get("agent") or {}
    target_pane = agent.get("pane_id")
    if target_pane == caller["caller_pane"]:
        raise WatchError("cannot delegate to the calling agent itself")
    if agent.get("agent_status") not in READY_STATES:
        raise WatchError(f"the target agent is not ready for input (status={agent.get('agent_status')})")
    request = {"kind": "delegate", "label": label, "target": target, "target_pane": target_pane,
               "target_session": agent.get("agent_session"), **caller, "created_at": _now()}
    run_dir = _create_run(label, request)
    try:
        _herdr("agent", "prompt", target, f"{tag}\n{prompt.rstrip()}{_reply_instruction(run_dir)}")
    except WatchError as exc:
        _record(run_dir, "delegate_failed", error=str(exc))
        raise
    _record(run_dir, "delegated", target=target, target_pane=target_pane)
    return {"run_dir": str(run_dir), "target": target, "target_pane": target_pane,
            "caller_pane": caller["caller_pane"]}


def reply(run_dir: Path, report: Path) -> dict[str, Any]:
    request = json.loads((run_dir / "request.json").read_text())
    if request.get("kind") != "delegate":
        raise WatchError("this run is not a delegation")
    report = report.resolve()
    if not report.is_file():
        raise WatchError(f"report file not found: {report}")
    sender = _caller()
    tag = _tag("reply", sender["caller_session"])
    with (run_dir / "replies.jsonl").open("a") as replies:
        replies.write(json.dumps({"report": str(report), "sender_pane": sender["caller_pane"],
                                  "sender_session": sender["caller_session"], "replied_at": _now()},
                                 ensure_ascii=False) + "\n")
    _record(run_dir, "reply_received", report=str(report), sender_pane=sender["caller_pane"])
    # The delegate ends its turn now; the notifier waits for the caller in the background.
    with (run_dir / "notifier.log").open("ab") as log:
        subprocess.Popen([sys.executable, str(SCRIPT), "notify-reply", str(run_dir), str(report), tag],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    _record(run_dir, "notifier_started")
    return {"run_dir": str(run_dir), "report": str(report), "caller_pane": request["caller_pane"]}


def notify_reply(run_dir: Path, report: str, tag: str) -> int:
    request = json.loads((run_dir / "request.json").read_text())
    message = (f"{tag} {request['target']}（{request['target_pane']}）から"
               f"「{request['label']}」の返信が届いた。報告: {report} 記録: {run_dir}")
    return _notify_and_record(run_dir, request, message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    start_parser = sub.add_parser("start", help="open a watcher pane and return immediately")
    start_parser.add_argument("--label", required=True, help="what is being waited for")
    start_parser.add_argument("--timeout", type=int, help="seconds before the command is terminated")
    start_parser.add_argument("--cwd", default=os.getcwd(), help="working directory of the command")
    start_parser.add_argument("command", nargs=argparse.REMAINDER, help="-- command [args...]")
    run_parser = sub.add_parser("run", help="internal: executed inside the watcher pane")
    run_parser.add_argument("run_dir", type=Path)
    delegate_parser = sub.add_parser("delegate", help="send a task to another agent and return immediately")
    delegate_parser.add_argument("--to", required=True, help="live agent name or pane id")
    delegate_parser.add_argument("--label", required=True, help="what is being delegated")
    task = delegate_parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--prompt", help="task text")
    task.add_argument("--prompt-file", type=Path, help="file containing the task text")
    reply_parser = sub.add_parser("reply", help="return a report to the delegating agent")
    reply_parser.add_argument("run_dir", type=Path)
    reply_parser.add_argument("--report", type=Path, required=True, help="Markdown report file")
    notify_parser = sub.add_parser("notify-reply", help="internal: delivers a reply in the background")
    notify_parser.add_argument("run_dir", type=Path)
    notify_parser.add_argument("report")
    notify_parser.add_argument("tag")
    args = parser.parse_args(argv)
    try:
        if args.action == "start":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            print(json.dumps(start(args.label, command, args.cwd, args.timeout), ensure_ascii=False))
            return 0
        if args.action == "delegate":
            prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text()
            print(json.dumps(delegate(args.label, args.to, prompt), ensure_ascii=False))
            return 0
        if args.action == "reply":
            print(json.dumps(reply(args.run_dir, args.report), ensure_ascii=False))
            return 0
        if args.action == "notify-reply":
            return notify_reply(args.run_dir, args.report, args.tag)
        return run(args.run_dir, sys.stdout.buffer)
    except WatchError as exc:
        print(f"herdr-watch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
