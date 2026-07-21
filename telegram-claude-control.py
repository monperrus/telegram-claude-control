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
import subprocess
import sys
import threading
import time
import urllib.parse

CONFIG_PATH = os.environ.get("TELEGRAM_CLAUDE_CONFIG", os.path.expanduser("~/.config/telegram-claude-control.env"))
STATE_PATH = os.environ.get("TELEGRAM_CLAUDE_STATE", os.path.expanduser("~/.local/state/telegram-claude-control.json"))
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
# /bg jobs run detached from the ASK_TIMEOUT-bound foreground path (see
# ClaudeHeadless.start_background), so they get their own, much longer cap.
BG_TIMEOUT = int(os.environ.get("TELEGRAM_CLAUDE_BG_TIMEOUT", "14400"))
# Inline the diff in the edited-file notification when total changed lines
# (added + removed) is below this. Above it, only the +/- counts are shown.
DIFF_PREVIEW_MAX_LINES = int(os.environ.get("TELEGRAM_CLAUDE_DIFF_PREVIEW_LINES", "10"))


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
    time -- otherwise one /bg job would freeze every other topic for as long
    as it runs."""

    def __init__(self, sessions=None, on_change=None):
        self.sessions = sessions if sessions is not None else {}
        self.on_change = on_change or (lambda: None)
        self._locks_guard = threading.Lock()
        self._conversation_locks = {}
        self._jobs_lock = threading.Lock()
        self._job_seq = 0
        self.jobs = {}

    def _conversation_lock(self, conversation_id):
        key = str(conversation_id)
        with self._locks_guard:
            return self._conversation_locks.setdefault(key, threading.Lock())

    def _execute(self, prompt, conversation_id, notify, timeout, on_process=None):
        """One `claude -p` turn: spawns the process, parses its JSONL event
        stream (calling notify(text) per completed file-editing tool call),
        and returns the final assistant result text. Shared by the
        foreground /ask path and background /bg jobs -- they differ only in
        their timeout and in how the caller is allowed to observe/cancel the
        process (on_process)."""
        args = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        session_id = self.sessions.get(str(conversation_id))
        if session_id:
            args += ["--resume", session_id]
        if PERMISSION_MODE:
            args += ["--permission-mode", PERMISSION_MODE]
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
        (no blocking) if a /bg job already owns this conversation."""
        notify = notify or (lambda text: None)
        lock = self._conversation_lock(conversation_id)
        if not lock.acquire(blocking=False):
            raise RuntimeError("A request is already running for this conversation. Check /jobs, or wait for it to finish.")
        try:
            return self._execute(prompt, conversation_id, notify, ASK_TIMEOUT)
        finally:
            lock.release()

    def start_background(self, prompt, conversation_id, thread_id, notify):
        """Starts prompt as a background job that is not bound by
        ASK_TIMEOUT: it runs on a daemon thread owned by the long-lived
        controller process itself, so it survives well past any single
        `claude -p` process's lifetime. notify(text) is used both for
        progress messages and for the final completion/failure message,
        exactly like the foreground path. Returns the new job_id, or None if
        this conversation is already busy (foreground or another /bg job)."""
        lock = self._conversation_lock(conversation_id)
        if not lock.acquire(blocking=False):
            return None
        with self._jobs_lock:
            self._job_seq += 1
            job_id = f"bg{self._job_seq}"
            self.jobs[job_id] = {"conversation_id": conversation_id, "thread_id": thread_id, "prompt": prompt, "started": time.time(), "process": None}

        def on_process(process):
            with self._jobs_lock:
                if job_id in self.jobs:
                    self.jobs[job_id]["process"] = process

        def worker():
            try:
                result = self._execute(prompt, conversation_id, notify, BG_TIMEOUT, on_process=on_process)
                notify(f"✅ Background job {job_id} finished:\n{result}")
            except Exception as error:
                notify(f"❌ Background job {job_id} failed: {_friendly_claude_error(error)}")
            finally:
                with self._jobs_lock:
                    self.jobs.pop(job_id, None)
                lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return job_id

    def list_jobs(self):
        with self._jobs_lock:
            return [(job_id, dict(job)) for job_id, job in self.jobs.items()]

    def cancel_job(self, job_id):
        with self._jobs_lock:
            job = self.jobs.get(job_id)
        if job is None or job["process"] is None:
            return False
        job["process"].kill()
        return True

    def reset(self, conversation_id=None):
        with STATE_LOCK:
            removed = self.sessions.pop(str(conversation_id), None) is not None
        if removed:
            self.on_change()


CLAUDE = ClaudeHeadless()


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


def screen(lines=120):
    result = tmux("capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", SESSION)
    if result.returncode:
        return "tmux session is unavailable: " + result.stderr.strip()
    output = result.stdout.strip() or "(terminal is blank)"
    # Telegram messages are capped at 4096 characters.
    return output[-3800:]


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


def start_bg(chat_id, message_id, thread_id, prompt):
    """Start a /bg job: unlike /ask, this is not bound by ASK_TIMEOUT, so a
    job can run for hours. notify (here, reply) delivers both progress and
    the final completion/failure message whenever the job actually ends."""
    job_id = CLAUDE.start_background(prompt, conversation_id=thread_id, thread_id=thread_id, notify=lambda text: reply(chat_id, text, thread_id))
    if job_id is None:
        reply(chat_id, "A request is already running for this conversation. Check /jobs, or wait for it to finish.", thread_id)
        return
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-claude-control: reaction failed: {error}", file=sys.stderr, flush=True)
    reply(chat_id, f"Started background job {job_id}. You'll get a message here when it finishes.", thread_id)


def jobs_summary():
    jobs = CLAUDE.list_jobs()
    if not jobs:
        return "No background jobs running."
    now = time.time()
    lines = [f"{job_id} ({int(now - job['started'])}s, thread {job['thread_id']}): {job['prompt'][:80]}" for job_id, job in jobs]
    return "\n".join(lines)


def cancel_job(job_id):
    return f"Cancelled {job_id}." if CLAUDE.cancel_job(job_id) else f"No running background job {job_id}."


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


def reply(chat_id, text, thread_id=None):
    # Plain text avoids Telegram markup interpretation of terminal output.
    payload = {"chat_id": chat_id, "text": text[:4096]}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    api("sendMessage", payload)


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
    "t": "/jobs",
}
# One letter plus " <argument>".
PREFIXED_SHORTCUTS = {
    "t": "/bg",
    "m": "/tmux",
    "x": "/sh",
    "c": "/ask",
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
            "/bg <prompt>: run headless `claude -p` in the background, not bound by the usual timeout; "
            "you get a message here the moment it finishes (or fails). "
            "/jobs: list running background jobs. /cancel <job_id>: kill one. "
            "/screen, /status, /interrupt. "
            "One-letter shortcuts: h=help s=status v=screen i=interrupt t=jobs (`t <prompt>`=/bg) "
            "m <text>=/tmux x <cmd>=/sh c <prompt>=/ask.",
            thread_id,
        )
    elif command == "/screen":
        reply(chat_id, screen(), thread_id)
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
    elif command == "/bg":
        reply(chat_id, "Usage: /bg <prompt>", thread_id)
    elif command.startswith("/bg "):
        bg_prompt = command[4:].strip()
        if bg_prompt:
            start_bg(chat_id, message["message_id"], thread_id, bg_prompt)
        else:
            reply(chat_id, "Usage: /bg <prompt>", thread_id)
    elif command == "/jobs":
        reply(chat_id, jobs_summary(), thread_id)
    elif command == "/cancel":
        reply(chat_id, "Usage: /cancel <job_id>", thread_id)
    elif command.startswith("/cancel "):
        reply(chat_id, cancel_job(command[8:].strip()), thread_id)
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


def main():
    state = read_state()
    # Reuse the same dict object (not a copy) so mutations CLAUDE makes to it
    # are already reflected in the next write_state(state) call below.
    CLAUDE.sessions = state.setdefault("sessions", {})
    CLAUDE.on_change = lambda: write_state(state)
    try:
        # Establish the sender's TLS connection before the first user message,
        # making the immediate acknowledgement a reused connection.
        api("getMe")
    except Exception as error:
        print(f"telegram-claude-control: sender preflight failed: {error}", file=sys.stderr, flush=True)
    while True:
        try:
            updates = api("getUpdates", {"offset": state.get("offset", 0), "timeout": 30, "allowed_updates": json.dumps(["message"])})
            for update in updates:
                state["offset"] = update["update_id"] + 1
                handle(update.get("message", {}), state)
                write_state(state)
        except KeyboardInterrupt:
            return
        except Exception as error:
            print(f"telegram-claude-control: {error}", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
