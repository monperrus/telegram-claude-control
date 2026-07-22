#!/usr/bin/env python3
"""Private Telegram controller for a tmux-hosted Claude Code session and the
local `claude -p` headless CLI. Modeled on
https://github.com/monperrus/telegram-tmux-controller, swapping that
project's Codex app-server JSONL client for Claude Code's documented
non-interactive mode (`claude -p --output-format stream-json --verbose
[--resume <id>]`), parsing the JSONL event stream so each completed
file-editing tool call reaches Telegram as its own message."""
import difflib
import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

CONFIG_PATH = os.environ.get("TELEGRAM_CLAUDE_CONFIG", os.path.expanduser("~/.config/telegram-claude-control.env"))
STATE_PATH = os.environ.get("TELEGRAM_CLAUDE_STATE", os.path.expanduser("~/.local/state/telegram-claude-control.json"))
JOBS_DB_PATH = os.environ.get("TELEGRAM_CLAUDE_JOBS_DB", os.path.expanduser("~/.local/state/telegram-claude-control-jobs.db"))
SESSION = os.environ.get("TELEGRAM_CLAUDE_SESSION", "claude")
WORKSPACE = os.environ.get("TELEGRAM_CLAUDE_WORKSPACE", os.path.expanduser("~"))
CLAUDE_BIN = os.environ.get("TELEGRAM_CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
# Left unset by default: headless calls then inherit whatever permission mode
# is configured in ~/.claude/settings.json on this host. Set this to
# constrain headless requests (e.g. "plan" or "acceptEdits") independent of
# that global setting.
PERMISSION_MODE = os.environ.get("TELEGRAM_CLAUDE_PERMISSION_MODE", "")
ASK_TIMEOUT = int(os.environ.get("TELEGRAM_CLAUDE_ASK_TIMEOUT", "600"))
SH_TIMEOUT = int(os.environ.get("TELEGRAM_CLAUDE_SH_TIMEOUT", "60"))
# /task jobs run detached from the ASK_TIMEOUT-bound foreground path (see
# ClaudeHeadless.start_background), so they get their own, much longer cap.
TASK_TIMEOUT = int(os.environ.get("TELEGRAM_CLAUDE_TASK_TIMEOUT", "14400"))
# Inline the diff in the edited-file notification when total changed lines
# (added + removed) is below this. Above it, only the +/- counts are shown.
DIFF_PREVIEW_MAX_LINES = int(os.environ.get("TELEGRAM_CLAUDE_DIFF_PREVIEW_LINES", "10"))
# /usage runs claude -p "/usage" directly: a free, instant, local-only
# meta-command (no API tokens, no --resume needed), so it gets its own short
# timeout instead of reusing ASK_TIMEOUT.
USAGE_TIMEOUT = int(os.environ.get("TELEGRAM_CLAUDE_USAGE_TIMEOUT", "30"))
# systemd --user unit installed by install.sh; /restart targets this.
UNIT_NAME = os.environ.get("TELEGRAM_CLAUDE_UNIT", "telegram-claude-controller.service")
# /model button shortcuts; /model <any other id> is also accepted verbatim.
MODEL_PRESETS = ["sonnet", "opus", "haiku"]

# Registered with Telegram via setMyCommands so typing "/" brings up a
# tappable, autocompleted menu (with descriptions) in any Telegram client --
# in addition to the /help message's own inline buttons for the
# no-argument commands. /screen_show, driven exclusively by /screen's own
# buttons, is left out, same as it's left out of /help's text.
BOT_COMMANDS = [
    ("help", "Show available commands"),
    ("ask", "Headless claude -p with a prompt (or just send plain text)"),
    ("newsession", "Forget this conversation and start fresh"),
    ("model", "Show/pick the Claude model for this conversation"),
    ("usage", "Current session/weekly usage against your plan limits"),
    ("task", "Run claude -p in the background, no timeout"),
    ("tasks", "List recent background jobs"),
    ("cancel", "Cancel a running background job"),
    ("sh", "Run a shell command directly"),
    ("tmux", "Type text into the interactive tmux session"),
    ("screen", "Capture tmux session/window/pane output"),
    ("status", "Report tmux session status"),
    ("interrupt", "Send Ctrl-C to the tmux session"),
    ("restart", "Restart the controller service"),
]


def config():
    values = {}
    with open(CONFIG_PATH, encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    for required in ("BOT_TOKEN", "PAIR_CODE"):
        if not values.get(required):
            raise RuntimeError(f"Missing {required} in {CONFIG_PATH}")
    return values


CFG = config()
API_HOST = "api.telegram.org"
API_PREFIX = f"/bot{CFG['BOT_TOKEN']}/"


# Tool names whose completed tool_result should trigger one Telegram message
# per invocation (default: file-editing tools, one message per edited file).
FILE_EDIT_TOOLS = {
    "Write": "Wrote",
    "Edit": "Edited",
    "MultiEdit": "Edited (multi-part)",
    "NotebookEdit": "Edited notebook",
}


def _tool_target_path(name, tool_input):
    if name == "NotebookEdit":
        return tool_input.get("notebook_path", "?")
    return tool_input.get("file_path", "?")


def _compact_diff_line(line):
    """A raw unified-diff +/- line, with its content's indentation and
    trailing whitespace stripped so it fits a phone screen."""
    marker, content = line[0], line[1:]
    return f"{marker} {content.strip()}"


def _text_diff(old_text, new_text):
    """(added, removed, compact +/- lines) between two text blobs, context
    lines omitted so only actually-changed lines are counted and shown."""
    diff = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), n=0, lineterm="")
    changes = [line for line in diff if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))]
    added = sum(1 for line in changes if line.startswith("+"))
    removed = sum(1 for line in changes if line.startswith("-"))
    return added, removed, [_compact_diff_line(line) for line in changes]


def _edit_stats(name, tool_input):
    """(added, removed, preview lines) for a completed file-editing tool
    call. Edit/MultiEdit get a real before/after diff since both strings are
    right there in the tool call. Write/NotebookEdit only ever see the new
    content, so they're reported as pure additions."""
    if name == "Edit":
        return _text_diff(tool_input.get("old_string", ""), tool_input.get("new_string", ""))
    if name == "MultiEdit":
        added = removed = 0
        preview = []
        for edit in tool_input.get("edits", []):
            a, r, lines = _text_diff(edit.get("old_string", ""), edit.get("new_string", ""))
            added += a
            removed += r
            preview += lines
        return added, removed, preview
    if name == "NotebookEdit" and tool_input.get("edit_mode") == "delete":
        return 0, 0, []
    content = tool_input.get("content") if name == "Write" else tool_input.get("new_source", "")
    lines = (content or "").splitlines()
    return len(lines), 0, [_compact_diff_line("+" + line) for line in lines]


class ClaudeHeadless:
    """Runs prompts through `claude -p --output-format stream-json`, one
    process per turn, parsing the JSONL event stream so callers can get a
    Telegram message per completed tool call (e.g. per edited file) instead
    of waiting for the whole turn. Conversation continuity across turns comes
    from Claude Code's own --resume <session_id>, not from a long-lived
    subprocess.

    Session ids are kept per conversation_id (Telegram's message_thread_id,
    or None outside of forum topics) so parallel Telegram threads don't
    share -- or clobber -- each other's Claude context. The mapping is
    persisted (via on_change) so a thread's context survives a controller
    restart; only an explicit /newsession clears it early.

    Locking is per conversation_id, not global: a claude -p session cannot
    safely be driven by two concurrent processes sharing the same --resume
    id, but unrelated Telegram topics must still be able to run at the same
    time -- otherwise one /task job would freeze every other topic for as
    long as it runs."""

    def __init__(self, sessions=None, models=None, on_change=None):
        self.sessions = sessions if sessions is not None else {}
        self.models = models if models is not None else {}
        self.on_change = on_change or (lambda: None)
        self._locks_guard = threading.Lock()
        self._conversation_locks = {}
        self._jobs_lock = threading.Lock()
        self.jobs = {}

    def set_model(self, conversation_id, model):
        """model=None resets the conversation to the CLI's own default."""
        with STATE_LOCK:
            if model:
                self.models[str(conversation_id)] = model
            else:
                self.models.pop(str(conversation_id), None)
        self.on_change()

    def get_model(self, conversation_id):
        return self.models.get(str(conversation_id))

    def _conversation_lock(self, conversation_id):
        key = str(conversation_id)
        with self._locks_guard:
            return self._conversation_locks.setdefault(key, threading.Lock())

    def _execute(self, prompt, conversation_id, notify, timeout, on_process=None):
        """One `claude -p` turn: spawns the process, parses its JSONL event
        stream (calling notify(text) per completed file-editing tool call),
        and returns the final assistant result text. Shared by the
        foreground /ask path and background /task jobs -- they differ only in
        their timeout and in how the caller is allowed to observe/cancel the
        process (on_process)."""
        args = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        session_id = self.sessions.get(str(conversation_id))
        if session_id:
            args += ["--resume", session_id]
        if PERMISSION_MODE:
            args += ["--permission-mode", PERMISSION_MODE]
        model = self.models.get(str(conversation_id))
        if model:
            args += ["--model", model]
        process = subprocess.Popen(
            args,
            cwd=WORKSPACE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if on_process:
            on_process(process)
        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            process.kill()

        timer = threading.Timer(timeout, on_timeout)
        timer.start()
        pending_tools = {}
        final_result = None
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                event_type = event.get("type")
                if event_type == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            pending_tools[block["id"]] = (block.get("name"), block.get("input", {}))
                elif event_type == "user":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") != "tool_result":
                            continue
                        name, tool_input = pending_tools.pop(block.get("tool_use_id"), (None, {}))
                        if name not in FILE_EDIT_TOOLS:
                            continue
                        path = _tool_target_path(name, tool_input)
                        if block.get("is_error"):
                            error_text = str(block.get("content", ""))[:300]
                            notify(f"❌ {name} failed on {path}: {error_text}")
                            continue
                        added, removed, preview = _edit_stats(name, tool_input)
                        header = f"✏️ {FILE_EDIT_TOOLS[name]} {path} (+{added} -{removed})"
                        if preview and added + removed < DIFF_PREVIEW_MAX_LINES:
                            notify(header + "\n" + "\n".join(preview))
                        else:
                            notify(header)
                elif event_type == "result":
                    final_result = event
            process.wait(timeout=10)
        finally:
            timer.cancel()
        stderr_text = process.stderr.read()
        if final_result is None:
            if timed_out.is_set():
                raise RuntimeError(f"claude timed out after {timeout}s")
            raise RuntimeError((stderr_text or "claude exited without a result event").strip()[:1500])
        if final_result.get("session_id"):
            with STATE_LOCK:
                self.sessions[str(conversation_id)] = final_result["session_id"]
            self.on_change()
        if final_result.get("is_error"):
            raise RuntimeError(str(final_result.get("result") or "claude reported an error"))
        return str(final_result.get("result") or "(empty response)")

    def run(self, prompt, conversation_id=None, notify=None):
        """Runs one foreground turn, calling notify(text) for each completed
        file-editing tool call as it happens, and returning the final
        assistant result text once the turn completes. Raises immediately
        (no blocking) if a /task job already owns this conversation."""
        notify = notify or (lambda text: None)
        lock = self._conversation_lock(conversation_id)
        if not lock.acquire(blocking=False):
            raise RuntimeError("A request is already running for this conversation. Check /tasks, or wait for it to finish.")
        try:
            return self._execute(prompt, conversation_id, notify, ASK_TIMEOUT)
        finally:
            lock.release()

    def start_background(self, prompt, conversation_id, thread_id, notify, on_start=None, on_done=None):
        """Starts prompt as a background job that is not bound by
        ASK_TIMEOUT: it runs on a daemon thread owned by the long-lived
        controller process itself, so it survives well past any single
        `claude -p` process's lifetime. notify(text) is used both for
        progress messages and for the final completion/failure message,
        exactly like the foreground path. on_start(job_id) fires
        synchronously, before the worker thread launches (so a caller that
        wants to durably record "job started" can't race the job finishing
        first); on_done(job_id, status, text) fires exactly once, with
        status one of "completed"/"failed"/"cancelled". Returns the new
        job_id, or None if this conversation is already busy (foreground or
        another /task job)."""
        on_start = on_start or (lambda job_id: None)
        on_done = on_done or (lambda job_id, status, text: None)
        lock = self._conversation_lock(conversation_id)
        if not lock.acquire(blocking=False):
            return None
        # uuid4, not a sequence counter: self._job_seq resets to 0 on every
        # restart, but JOBS (SQLite) is durable across restarts, so a
        # sequential id would collide with a still-present historical row
        # (UNIQUE constraint failure) as soon as a job ran before the restart.
        job_id = f"bg{uuid.uuid4().hex[:8]}"
        with self._jobs_lock:
            self.jobs[job_id] = {"conversation_id": conversation_id, "thread_id": thread_id, "prompt": prompt, "started": time.time(), "process": None, "cancelled": False}
        try:
            on_start(job_id)
        except Exception:
            # on_start (JOBS.start) can still fail for other reasons (e.g. a
            # locked db file). Without this, the lock/registry entry above
            # would leak permanently -- silently blocking every future /ask
            # and /task on this conversation with "already running" even
            # though no job is actually running.
            with self._jobs_lock:
                self.jobs.pop(job_id, None)
            lock.release()
            raise

        def on_process(process):
            with self._jobs_lock:
                if job_id in self.jobs:
                    self.jobs[job_id]["process"] = process

        def worker():
            try:
                result = self._execute(prompt, conversation_id, notify, TASK_TIMEOUT, on_process=on_process)
                notify(f"✅ Background job {job_id} finished:\n{result}")
                on_done(job_id, "completed", result)
            except Exception as error:
                with self._jobs_lock:
                    cancelled = self.jobs.get(job_id, {}).get("cancelled", False)
                if cancelled:
                    notify(f"🚫 Background job {job_id} was cancelled.")
                    on_done(job_id, "cancelled", str(error))
                else:
                    friendly = _friendly_claude_error(error)
                    notify(f"❌ Background job {job_id} failed: {friendly}")
                    on_done(job_id, "failed", friendly)
            finally:
                with self._jobs_lock:
                    self.jobs.pop(job_id, None)
                lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return job_id

    def cancel_job(self, job_id):
        with self._jobs_lock:
            job = self.jobs.get(job_id)
            if job is None or job["process"] is None:
                return False
            job["cancelled"] = True
        job["process"].kill()
        return True

    def reset(self, conversation_id=None):
        with STATE_LOCK:
            removed = self.sessions.pop(str(conversation_id), None) is not None
        if removed:
            self.on_change()


CLAUDE = ClaudeHeadless()


class JobStore:
    """Durable log of /task jobs (SQLite) so /tasks history and status survive
    a controller restart -- unlike ClaudeHeadless's in-memory registry,
    which only knows about jobs from the current process's lifetime. This is
    a log, not a resumable queue: the underlying claude -p subprocess itself
    cannot survive a restart, so any row still marked "running" at startup
    is flagged "interrupted" (the conversation itself can still be continued
    normally via --resume with a fresh /ask or /task -- only that specific
    job's progress tracking was lost)."""

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._local = threading.local()
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    chat_id TEXT,
                    thread_id TEXT,
                    prompt TEXT,
                    status TEXT,
                    created_at REAL,
                    finished_at REAL,
                    result TEXT
                )"""
            )
            db.execute("UPDATE jobs SET status = 'interrupted' WHERE status = 'running'")

    def _connect(self):
        # One connection per thread: sqlite3 connections aren't safe to share
        # across threads, and start_task/worker/handle() calls all come from
        # different ones.
        if not hasattr(self._local, "db"):
            self._local.db = sqlite3.connect(self.path, timeout=30)
        return self._local.db

    def start(self, job_id, chat_id, thread_id, prompt):
        with self._connect() as db:
            db.execute(
                "INSERT INTO jobs (job_id, chat_id, thread_id, prompt, status, created_at) VALUES (?, ?, ?, ?, 'running', ?)",
                (job_id, str(chat_id), str(thread_id), prompt, time.time()),
            )

    def finish(self, job_id, status, result):
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status = ?, result = ?, finished_at = ? WHERE job_id = ?",
                (status, (result or "")[:1500], time.time(), job_id),
            )

    def recent(self, limit=8):
        with self._connect() as db:
            return db.execute(
                "SELECT job_id, status, prompt, created_at, finished_at FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def get(self, job_id):
        with self._connect() as db:
            return db.execute(
                "SELECT job_id, chat_id, thread_id, prompt, status, created_at, finished_at, result FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()


JOBS = JobStore(JOBS_DB_PATH)


class TelegramApi:
    """Small keep-alive Telegram client; one instance per request lane."""

    def __init__(self):
        self.connection = None
        self.lock = threading.Lock()

    def _connect(self):
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(API_HOST, timeout=40)
        return self.connection

    def _discard_connection(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = None

    def call(self, method, payload=None):
        data = urllib.parse.urlencode(payload or {}).encode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(data)),
            "Connection": "keep-alive",
        }
        # A server may close an idle keep-alive connection. Reconnect once so
        # that an idle bot does not fail its first reply. A 409 means another
        # getUpdates call briefly overlapped this one (e.g. right after a
        # restart) -- also worth one retry rather than surfacing as an error.
        with self.lock:
            for attempt in range(2):
                try:
                    connection = self._connect()
                    connection.request("POST", API_PREFIX + method, data, headers)
                    response = connection.getresponse()
                    body = response.read()
                    if response.status == 409 and not attempt:
                        self._discard_connection()
                        continue
                    if response.status >= 400:
                        raise RuntimeError(f"Telegram API returned HTTP {response.status}")
                    result = json.loads(body)
                    if not result.get("ok"):
                        raise RuntimeError(result.get("description", "Telegram API request failed"))
                    return result["result"]
                except (http.client.HTTPException, OSError):
                    self._discard_connection()
                    if attempt:
                        raise


# Keep long polling separate: an in-flight getUpdates request must never delay
# acknowledgements or final replies from worker threads.
POLL_API = TelegramApi()
SEND_API = TelegramApi()


def api(method, payload=None):
    client = POLL_API if method == "getUpdates" else SEND_API
    return client.call(method, payload)


def _friendly_claude_error(error):
    """Best-effort translation of a raw claude -p failure into short,
    Telegram-friendly text -- recognized failure classes (rate limits,
    overload) get a plain explanation instead of a raw stack/API dump."""
    text = str(error).strip()
    lowered = text.lower()
    if "429" in text or "rate limit" in lowered:
        return "Claude's rate limit was hit (HTTP 429). No reply was produced -- please try again later."
    if "usage limit" in lowered:
        return "Claude's usage limit was reached. Please try again later."
    if "529" in text or "overloaded" in lowered:
        return "Claude's API is overloaded right now. Please try again shortly."
    return text or "claude -p request failed."


class TypingIndicator:
    """Keeps a chat's "typing..." indicator up for the duration of a turn:
    Telegram only shows it for a few seconds per call, so this refreshes it
    on a timer (and on demand, e.g. per completed tool call) until stop()."""

    INTERVAL = 4

    def __init__(self, chat_id, thread_id):
        self.chat_id = chat_id
        self.thread_id = thread_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def ping(self):
        try:
            payload = {"chat_id": self.chat_id, "action": "typing"}
            if self.thread_id is not None:
                payload["message_thread_id"] = self.thread_id
            api("sendChatAction", payload)
        except Exception as error:
            print(f"telegram-claude-control: typing indicator failed: {error}", file=sys.stderr, flush=True)

    def _run(self):
        while not self._stop.wait(self.INTERVAL):
            self.ping()

    def __enter__(self):
        self.ping()
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        self._thread.join(timeout=1)


# Guards both the on-disk state file and the in-memory `sessions` dict it
# mirrors, so a background `claude -p` turn finishing (mutating sessions) can
# never race the main loop's per-update write_state() into a corrupt file or
# a "dictionary changed size during iteration" error.
STATE_LOCK = threading.Lock()


def read_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {"offset": 0}


def write_state(state):
    with STATE_LOCK:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        temporary = STATE_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(state, file)
        os.chmod(temporary, 0o600)
        os.replace(temporary, STATE_PATH)


def tmux(*args, input_text=None):
    return subprocess.run(["tmux", *args], input=input_text, text=True, capture_output=True, timeout=15)


def screen(lines=120, target=None):
    result = tmux("capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", target or SESSION)
    if result.returncode:
        return "tmux session is unavailable: " + result.stderr.strip()
    output = result.stdout.strip() or "(terminal is blank)"
    # Telegram messages are capped at 4096 characters.
    return output[-3800:]


def list_tmux_sessions():
    result = tmux("list-sessions", "-F", "#{session_name}")
    return [line for line in result.stdout.splitlines() if line] if result.returncode == 0 else []


def list_tmux_windows(session):
    """[(window_target, window_name), ...] for every window in session."""
    result = tmux("list-windows", "-t", session, "-F", "#{session_name}:#{window_index} #{window_name}")
    if result.returncode:
        return []
    pairs = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        target, _, name = line.partition(" ")
        pairs.append((target, name or target))
    return pairs


def list_tmux_panes(window_target):
    """[(pane_target, pane_title), ...] for every pane in window_target."""
    result = tmux("list-panes", "-t", window_target, "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_title}")
    if result.returncode:
        return []
    pairs = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        target, _, title = line.partition(" ")
        pairs.append((target, title or target))
    return pairs


def screen_entry():
    """(text, buttons) listing every pane across the whole tmux server as a
    button -- always, even if there's only one -- so tapping a screen is
    always exactly one tap away from its content, with no auto-skipped
    levels to cause surprises. callback_data is "/screen_show <target>"."""
    buttons = []
    for session in list_tmux_sessions():
        for window_target, _ in list_tmux_windows(session):
            for pane_target, pane_title in list_tmux_panes(window_target):
                label = f"{pane_target} — {pane_title}" if pane_title and pane_title != pane_target else pane_target
                buttons.append((label[:60], f"/screen_show {pane_target}"))
    if not buttons:
        return "tmux is unavailable: no panes found.", None
    return "Pick a screen:", buttons


def restart_controller():
    """Asks systemd to restart this service's unit and returns an error
    string, or None on success. --no-block makes systemctl return as soon as
    the job is queued instead of waiting for the stop+start cycle to finish
    -- which would otherwise mean waiting on systemd to SIGTERM this very
    process before the call could ever return."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "--no-block", "restart", UNIT_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as error:
        return str(error)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or f"systemctl exited {result.returncode}"
    return None


def delayed_screen(chat_id, thread_id):
    """Give an interactive Claude Code session time to respond, without
    pausing Telegram polling."""
    time.sleep(20)
    try:
        reply(chat_id, screen(lines=30), thread_id)
    except Exception as error:
        print(f"telegram-claude-control: delayed reply failed: {error}", file=sys.stderr, flush=True)


def run_ask(chat_id, thread_id, prompt):
    """Run a headless `claude -p` request on its own thread, relaying one
    Telegram message per edited file as the turn progresses. A "typing..."
    indicator stays up for the whole turn so a slow reply doesn't look like
    a dropped message, refreshed on each completed tool call."""
    with TypingIndicator(chat_id, thread_id) as typing:
        def notify(text):
            typing.ping()
            reply(chat_id, text, thread_id)

        try:
            answer = CLAUDE.run(prompt, conversation_id=thread_id, notify=notify)
            reply(chat_id, answer, thread_id)
        except Exception as error:
            print(f"telegram-claude-control: headless request failed: {error}", file=sys.stderr, flush=True)
            reply(chat_id, _friendly_claude_error(error), thread_id)


def start_task(chat_id, message_id, thread_id, prompt):
    """Start a /task job: unlike /ask, this is not bound by ASK_TIMEOUT, so a
    job can run for hours. notify (here, reply) delivers both progress and
    the final completion/failure message whenever the job actually ends.
    on_start/on_done record the job in JOBS (SQLite) so /tasks history and
    status survive a controller restart."""
    job_id = CLAUDE.start_background(
        prompt,
        conversation_id=thread_id,
        thread_id=thread_id,
        notify=lambda text: reply(chat_id, text, thread_id),
        on_start=lambda job_id: JOBS.start(job_id, chat_id, thread_id, prompt),
        on_done=lambda job_id, status, text: JOBS.finish(job_id, status, text),
    )
    if job_id is None:
        reply(chat_id, "A request is already running for this conversation. Check /tasks, or wait for it to finish.", thread_id)
        return
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-claude-control: reaction failed: {error}", file=sys.stderr, flush=True)
    reply(chat_id, f"Started background job {job_id}. You'll get a message here when it finishes.", thread_id)


STATUS_ICONS = {"running": "🏃", "completed": "✅", "failed": "❌", "cancelled": "🚫", "interrupted": "⚠️"}


def human_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def tasks_summary():
    """(text, buttons) for the 5 most recent /task jobs -- text shows all 5
    at a glance, and each also gets a button (callback_data "/tasks
    <job_id>") that drills into job_detail()'s duration/prompt/result view,
    the same as tapping through /screen's picker."""
    rows = JOBS.recent(5)
    if not rows:
        return "No background jobs yet.", None
    now = time.time()
    lines = []
    buttons = []
    for job_id, status, prompt, created_at, finished_at in rows:
        icon = STATUS_ICONS.get(status, "?")
        duration = human_duration((finished_at or now) - created_at)
        lines.append(f"{icon} {job_id} ({status}, {duration}): {prompt[:80]}")
        buttons.append((f"{icon} {job_id}", f"/tasks {job_id}"))
    return "\n".join(lines), buttons


def job_detail(job_id):
    row = JOBS.get(job_id)
    if row is None:
        return f"No job {job_id}."
    job_id, chat_id, thread_id, prompt, status, created_at, finished_at, result = row
    now = time.time()
    lines = [
        f"{STATUS_ICONS.get(status, '?')} {job_id} — {status}",
        f"Duration: {human_duration((finished_at or now) - created_at)}",
        f"Thread: {thread_id}",
        f"Prompt: {prompt[:800]}",
    ]
    if result:
        lines.append(f"Last message: {result[:1500]}")
    return "\n".join(lines)


def cancel_job(job_id):
    return f"Cancelled {job_id}." if CLAUDE.cancel_job(job_id) else f"No running background job {job_id}."


def usage_report():
    """Runs claude -p "/usage" -- a built-in Claude Code command
    (supportsNonInteractive) that reports current session/weekly usage
    against the account's plan limits. It's a local, no-token query (no
    --resume, no conversation state), so it's called directly rather than
    through ClaudeHeadless."""
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "/usage", "--output-format", "json"],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=USAGE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"/usage timed out after {USAGE_TIMEOUT}s"
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return (result.stderr or result.stdout).strip()[:1500] or f"/usage exited {result.returncode} with no output"
    if payload.get("is_error"):
        return str(payload.get("result") or "/usage reported an error")
    return str(payload.get("result") or "(empty /usage response)")


def run_shell(command):
    """Runs `command` directly (not through tmux) and returns its combined
    stdout/stderr, unlike /tmux which types into the long-lived interactive
    session."""
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=SH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {SH_TIMEOUT}s"
    output = (result.stdout + result.stderr).strip() or "(no output)"
    # Telegram messages are capped at 4096 characters.
    return f"$ {command} (exit {result.returncode})\n{output}"[:4096]


def run_sh(chat_id, thread_id, command):
    """Run a /sh shell command on its own thread so it can't block Telegram
    polling for up to SH_TIMEOUT seconds."""
    try:
        reply(chat_id, run_shell(command), thread_id)
    except Exception as error:
        print(f"telegram-claude-control: /sh failed: {error}", file=sys.stderr, flush=True)
        reply(chat_id, f"/sh failed: {error}", thread_id)


def send_terminal(text):
    typed = tmux("send-keys", "-t", SESSION, "-l", text)
    entered = tmux("send-keys", "-t", SESSION, "Enter")
    return typed.returncode == 0 and entered.returncode == 0


def reply(chat_id, text, thread_id=None, buttons=None, columns=1):
    """buttons, if given, is [(label, callback_data), ...], packed columns
    per row (default: one per row -- wide labels like tmux target names
    need the room; short ones, e.g. /help's action buttons, look better
    packed two or three per row)."""
    # Plain text avoids Telegram markup interpretation of terminal output.
    payload = {"chat_id": chat_id, "text": text[:4096]}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if buttons:
        rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
        keyboard = [[{"text": label, "callback_data": data} for label, data in row] for row in rows]
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    api("sendMessage", payload)


def register_bot_commands():
    """Registers BOT_COMMANDS with Telegram (setMyCommands) so every client
    shows a tappable "/" menu with descriptions, letting the user pick a
    command and just type its argument -- complements, but doesn't replace,
    /help's own inline buttons for the no-argument commands."""
    api("setMyCommands", {"commands": json.dumps([{"command": name, "description": description} for name, description in BOT_COMMANDS])})


def acknowledge(chat_id, message_id):
    """Mark an accepted request without adding a separate chat message."""
    api("setMessageReaction", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reaction": json.dumps([{"type": "emoji", "emoji": "\U0001fae1"}]),
    })


def start_ask(chat_id, message_id, thread_id, prompt):
    """Acknowledge promptly, then run the request asynchronously."""
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-claude-control: reaction failed: {error}", file=sys.stderr, flush=True)
    threading.Thread(target=run_ask, args=(chat_id, thread_id, prompt), daemon=True).start()


def start_sh(chat_id, message_id, thread_id, command):
    """Acknowledge promptly, then run the shell command asynchronously."""
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-claude-control: reaction failed: {error}", file=sys.stderr, flush=True)
    threading.Thread(target=run_sh, args=(chat_id, thread_id, command), daemon=True).start()


def permitted(chat_id, state):
    return str(state.get("chat_id", "")) == str(chat_id)


# Bare one-letter shortcuts (no argument).
ONE_LETTER_SHORTCUTS = {
    "h": "/help",
    "s": "/status",
    "v": "/screen",
    "i": "/interrupt",
    "r": "/restart",
    "t": "/tasks",
    "a": "/model",
    "u": "/usage",
}
# One letter plus " <argument>".
PREFIXED_SHORTCUTS = {
    "t": "/task",
    "m": "/tmux",
    "x": "/sh",
    "c": "/ask",
    "a": "/model",
}


def expand_shortcut(command):
    """Expands one-letter shortcuts (h/s/v/i/r/t/m/x/c) to their full command
    form. Only exact matches trigger: a message that happens to start with
    one of these letters but isn't "<letter>" or "<letter> <rest>" (e.g. an
    ordinary sentence) falls through unchanged to the normal /ask prompt
    path."""
    if command in ONE_LETTER_SHORTCUTS:
        return ONE_LETTER_SHORTCUTS[command]
    if len(command) > 2 and command[1] == " " and command[0] in PREFIXED_SHORTCUTS:
        return f"{PREFIXED_SHORTCUTS[command[0]]} {command[2:]}"
    return command


def handle(message, state):
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text")
    if chat_id is None or not text:
        return
    # Telegram forum topics each carry a message_thread_id; keying both the
    # Claude session and the reply's destination on it lets separate topics
    # hold separate, independently-resumed conversations instead of sharing
    # one Claude session and one reply thread across all of them.
    thread_id = message.get("message_thread_id")
    command = text.strip()
    if not state.get("chat_id"):
        if command.startswith("/pair ") and command[6:].strip() == CFG["PAIR_CODE"]:
            state["chat_id"] = chat_id
            reply(chat_id, "Paired. Send text for headless `claude -p`. Use /tmux <text> for the interactive terminal bridge. /help lists commands.", thread_id)
        else:
            reply(chat_id, "This private bot needs pairing. Send: /pair <your pairing code>", thread_id)
        return
    if not permitted(chat_id, state):
        # Do not reveal that a controller exists to unapproved chats.
        return
    command = expand_shortcut(command)
    if command in ("/start", "/help"):
        reply(
            chat_id,
            "Normal text or /ask <prompt>: headless `claude -p`, continued via --resume. "
            "You get one message per edited file as the turn runs, then a final summary. "
            "/newsession: forget the headless conversation and start fresh. "
            "/tmux <text>: terminal input into the interactive session, then last 30 lines after 20 seconds. "
            "/sh <command>: run a shell command directly and return its output. "
            "/task <prompt>: run headless `claude -p` in the background, not bound by the usual timeout; "
            "you get a message here the moment it finishes (or fails). "
            "/tasks: the 5 most recent background jobs, each tappable for its "
            "duration/prompt/last message (survives a restart). /tasks <job_id>: same detail directly. "
            "/cancel <job_id>: kill a running one. "
            "/restart: restart the controller's systemd service. "
            "/screen: every tmux pane on the server as a button, tap to see its content. "
            "/model: show/pick the Claude model for this conversation (sonnet/opus/haiku or any model id); "
            "/model default resets it. "
            "/usage: current session/weekly usage against your plan limits (free, instant, no tokens used). "
            "/status, /interrupt. "
            "One-letter shortcuts: h=help s=status v=screen i=interrupt r=restart t=tasks (`t <prompt>`=/task) "
            "a=model (`a <name>` selects) u=usage m <text>=/tmux x <cmd>=/sh c <prompt>=/ask. "
            "Tap a button below for the commands that need no arguments, or type / for Telegram's own "
            "command menu (with the rest, autocompleted so you can just add the argument).",
            thread_id,
            buttons=[
                ("usage", "/usage"), ("status", "/status"),
                ("screen", "/screen"), ("tasks", "/tasks"),
                ("model", "/model"), ("interrupt", "/interrupt"),
                ("newsession", "/newsession"), ("restart", "/restart"),
            ],
            columns=2,
        )
    elif command == "/usage":
        reply(chat_id, usage_report(), thread_id)
    elif command == "/screen":
        text, buttons = screen_entry()
        reply(chat_id, text, thread_id, buttons=buttons)
    elif command.startswith("/screen_show "):
        reply(chat_id, screen(target=command[13:].strip()), thread_id)
    elif command == "/status":
        result = tmux("has-session", "-t", SESSION)
        reply(chat_id, f"tmux session '{SESSION}': " + ("available" if result.returncode == 0 else "unavailable"), thread_id)
    elif command == "/interrupt":
        result = tmux("send-keys", "-t", SESSION, "C-c")
        if result.returncode == 0:
            try:
                acknowledge(chat_id, message["message_id"])
            except Exception as error:
                print(f"telegram-claude-control: reaction failed: {error}", file=sys.stderr, flush=True)
        else:
            reply(chat_id, "Unable to reach tmux.", thread_id)
    elif command == "/tmux":
        reply(chat_id, "Usage: /tmux <text>", thread_id)
    elif command.startswith("/tmux "):
        if send_terminal(command[6:]):
            threading.Thread(target=delayed_screen, args=(chat_id, thread_id), daemon=True).start()
        else:
            reply(chat_id, "Unable to reach the tmux session.", thread_id)
    elif command == "/sh":
        reply(chat_id, "Usage: /sh <command>", thread_id)
    elif command.startswith("/sh "):
        shell_command = command[4:].strip()
        if shell_command:
            start_sh(chat_id, message["message_id"], thread_id, shell_command)
        else:
            reply(chat_id, "Usage: /sh <command>", thread_id)
    elif command == "/task":
        reply(chat_id, "Usage: /task <prompt>", thread_id)
    elif command.startswith("/task "):
        task_prompt = command[6:].strip()
        if task_prompt:
            start_task(chat_id, message["message_id"], thread_id, task_prompt)
        else:
            reply(chat_id, "Usage: /task <prompt>", thread_id)
    elif command == "/tasks":
        text, buttons = tasks_summary()
        reply(chat_id, text, thread_id, buttons=buttons)
    elif command.startswith("/tasks "):
        reply(chat_id, job_detail(command[7:].strip()), thread_id)
    elif command == "/cancel":
        reply(chat_id, "Usage: /cancel <job_id>", thread_id)
    elif command.startswith("/cancel "):
        reply(chat_id, cancel_job(command[8:].strip()), thread_id)
    elif command == "/restart":
        # Reply (a blocking network call) completes before systemd's SIGTERM
        # can land, so the acknowledgment always reaches Telegram -- even
        # though this same process is what's about to be killed. Flush the
        # already-advanced offset first too, otherwise a SIGTERM landing
        # before main()'s post-dispatch write_state() would leave this same
        # /restart update unconsumed on disk -- replayed on every boot into
        # an infinite self-restart loop (this is what tripped systemd's
        # start-limit before this fix).
        reply(chat_id, "Restarting controller…", thread_id)
        write_state(state)
        error = restart_controller()
        if error:
            reply(chat_id, f"Restart failed: {error}", thread_id)
    elif command == "/model":
        current = CLAUDE.get_model(thread_id) or "(CLI default)"
        buttons = [(name, f"/model {name}") for name in MODEL_PRESETS] + [("reset to default", "/model default")]
        reply(chat_id, f"Model for this conversation: {current}\nPick one, or send /model <any model id>:", thread_id, buttons=buttons)
    elif command.startswith("/model "):
        model_name = command[7:].strip()
        if model_name in ("default", "reset", "clear", ""):
            CLAUDE.set_model(thread_id, None)
            reply(chat_id, "Model reset to the CLI default for this conversation.", thread_id)
        else:
            CLAUDE.set_model(thread_id, model_name)
            reply(chat_id, f"Model set to {model_name} for this conversation (takes effect next turn).", thread_id)
    elif command == "/newsession":
        CLAUDE.reset(thread_id)
        reply(chat_id, "Headless conversation cleared. Next message starts a new claude -p session.", thread_id)
    elif command == "/ask":
        reply(chat_id, "Usage: /ask <prompt>", thread_id)
    elif command.startswith("/ask "):
        prompt = command[5:].strip()
        if prompt:
            start_ask(chat_id, message["message_id"], thread_id, prompt)
        else:
            reply(chat_id, "Usage: /ask <prompt>", thread_id)
    elif command.startswith("/"):
        reply(chat_id, "Unknown command. Use /help.", thread_id)
    else:
        start_ask(chat_id, message["message_id"], thread_id, text)


def handle_callback(callback_query, state):
    """An inline-button tap: acknowledge it (required so Telegram stops
    showing a loading spinner on the button), then replay its callback_data
    through the same handle() dispatcher a typed command would use -- e.g. a
    /screen picker button's callback_data is literally "/screen_show ..."."""
    try:
        api("answerCallbackQuery", {"callback_query_id": callback_query["id"]})
    except Exception as error:
        print(f"telegram-claude-control: answerCallbackQuery failed: {error}", file=sys.stderr, flush=True)
    message = callback_query.get("message") or {}
    if not message.get("chat"):
        return
    synthetic = {
        "chat": message["chat"],
        "message_id": message.get("message_id"),
        "message_thread_id": message.get("message_thread_id"),
        "text": callback_query.get("data", ""),
    }
    handle(synthetic, state)


def main():
    state = read_state()
    # Reuse the same dict object (not a copy) so mutations CLAUDE makes to it
    # are already reflected in the next write_state(state) call below.
    CLAUDE.sessions = state.setdefault("sessions", {})
    CLAUDE.models = state.setdefault("models", {})
    CLAUDE.on_change = lambda: write_state(state)
    try:
        # Establish the sender's TLS connection before the first user message,
        # making the immediate acknowledgement a reused connection.
        api("getMe")
    except Exception as error:
        print(f"telegram-claude-control: sender preflight failed: {error}", file=sys.stderr, flush=True)
    try:
        register_bot_commands()
    except Exception as error:
        print(f"telegram-claude-control: setMyCommands failed: {error}", file=sys.stderr, flush=True)
    while True:
        try:
            updates = api("getUpdates", {"offset": state.get("offset", 0), "timeout": 30, "allowed_updates": json.dumps(["message", "callback_query"])})
            for update in updates:
                state["offset"] = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update["callback_query"], state)
                else:
                    handle(update.get("message", {}), state)
                write_state(state)
        except KeyboardInterrupt:
            return
        except Exception as error:
            print(f"telegram-claude-control: {error}", file=sys.stderr, flush=True)
            time.sleep(5)


def check():
    """Validates the install without entering the polling loop. Returns True
    if every fatal check passed (tmux session availability is a warning
    only, matching install docs -- an idle bot may just not have one yet)."""
    ok = True

    def report(passed, message, fatal=True):
        nonlocal ok
        print(("ok   " if passed else ("FAIL " if fatal else "warn ")) + message)
        if fatal:
            ok = ok and passed

    try:
        mode = oct(os.stat(CONFIG_PATH).st_mode & 0o777)
        report(mode == "0o600", f"config file is mode {mode} (want 0o600): {CONFIG_PATH}")
    except OSError as error:
        report(False, f"config file: {error}")

    report(shutil.which("tmux") is not None, "tmux is on PATH")
    report(os.access(CLAUDE_BIN, os.X_OK) or shutil.which(CLAUDE_BIN) is not None, f"claude binary is executable: {CLAUDE_BIN}")
    report(os.path.isdir(WORKSPACE), f"workspace directory exists: {WORKSPACE}")
    report(tmux("has-session", "-t", SESSION).returncode == 0, f"tmux session '{SESSION}' is available", fatal=False)

    try:
        api("getMe")
        report(True, "Telegram bot API connectivity (getMe)")
    except Exception as error:
        report(False, f"Telegram bot API connectivity: {error}")

    return ok


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        sys.exit(0 if check() else 1)
    main()
