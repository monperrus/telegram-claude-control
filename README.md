# Telegram Claude Controller

A private, single-user Telegram bot for controlling Claude Code on a machine
you own. Ported from [monperrus/telegram-tmux-controller](https://github.com/monperrus/telegram-tmux-controller):
same tmux bridge, same pairing/security model, but the Codex app-server JSONL
client is replaced with Claude Code's own documented non-interactive mode
(`claude -p --output-format json`, continued across turns with `--resume`)
instead of a reverse-engineered protocol. See
[`claude-code-remote-control.md`](claude-code-remote-control.md) for why this
repo does *not* talk to Anthropic's own Remote Control feature (claude.ai/code
↔ local CLI bridge) — that's undocumented and gated behind claude.ai OAuth in
a way its ToS reserves for Anthropic's own clients. This bot instead drives
the `claude` binary directly, the same way you would from a terminal.

## What it does

| Telegram input | Result |
| --- | --- |
| Normal text or `/ask <prompt>` | Runs `claude -p` headless (`--output-format stream-json`), continuing the same Claude Code session via `--resume` across turns. |
| `/newsession` | Forgets the headless session ID for this conversation; the next message starts a fresh `claude -p` session. |
| `/tmux <text>` | Types text and Enter into an interactive `claude` session running in the configured tmux window, then returns its recent output. |
| `/sh <command>` | Runs a shell command directly (not through tmux) and returns its combined stdout/stderr. |
| `/bg <prompt>` | Runs `claude -p` headless in the background, not bound by the `/ask` timeout — a message with the result (or failure) lands here whenever the job actually finishes, even hours later. |
| `/jobs` | Lists running background jobs (id, elapsed time, prompt). |
| `/cancel <job_id>` | Kills a running background job. |
| `/screen` | Returns recent output from that tmux session. |
| `/status` | Reports whether the tmux session exists. |
| `/interrupt` | Sends Ctrl-C to the tmux session. |

While a headless turn runs, the bot streams progress instead of going quiet
until it's done: every completed `Write`/`Edit`/`MultiEdit`/`NotebookEdit`
tool call sends its own Telegram message, e.g.

```
✏️ Edited src/app.py (+4 -2)
+ def handler(request):
+ return respond(request)
- def handler():
- return respond()
```

Diffs come straight from the tool call's own before/after strings (no extra
file reads), with each changed line's indentation and trailing whitespace
stripped to stay readable on a phone. When a change touches
`TELEGRAM_CLAUDE_DIFF_PREVIEW_LINES` (default 10) lines or more, only the
`(+added -removed)` counts are shown, not the full diff. A 🫡 reaction on
your message acknowledges receipt immediately, before the turn finishes.

The first chat must pair using a secret pairing code. Once paired, messages
from all other chats are silently ignored. In Telegram forum groups, each
topic (`message_thread_id`) gets its own independent `claude -p` session and
replies land back in that same topic, so parallel topics never share or
clobber each other's Claude conversation.

Only one request (foreground `/ask` or background `/bg`) runs per topic at a
time, since two processes can't safely share the same `--resume` session —
a second request on the same topic is rejected with a "already running"
reply rather than queued. Different topics run fully in parallel, so a
long `/bg` job in one topic never blocks `/ask` in another.

## Requirements

- Python 3.9+; standard library only, no dependencies to install.
- `tmux` on `PATH` (only needed for the `/tmux`, `/screen`, `/status`,
  `/interrupt` commands).
- `claude` (Claude Code CLI) installed and authenticated on this host.
- A Telegram bot token from [BotFather](https://t.me/BotFather).

## Install

1. Create a config file with restricted permissions:

   ```sh
   mkdir -p ~/.config ~/.local/state
   cp telegram-claude-control.env.example ~/.config/telegram-claude-control.env
   chmod 600 ~/.config/telegram-claude-control.env
   ```

2. Set `BOT_TOKEN` and a long, random `PAIR_CODE` in that file.
3. If you want the `/tmux` bridge, start an interactive Claude Code session
   in a named tmux session (default name `claude`):

   ```sh
   tmux new-session -d -s claude "claude"
   ```

4. Run the controller manually first:

   ```sh
   ./telegram-claude-control.py
   ```

5. In Telegram, send `/pair <your pairing code>` from the one chat that
   should control the bot. Use `/help` to confirm it's working.

For a persistent service, see
[systemd/telegram-claude-controller.service.example](systemd/telegram-claude-controller.service.example).

## Configuration

The config file (`~/.config/telegram-claude-control.env` by default) accepts
`KEY=value` lines and must contain:

- `BOT_TOKEN`: Telegram HTTP API token. Treat it like a password.
- `PAIR_CODE`: one-time pairing secret. Use a unique, high-entropy value.

Optional environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_CLAUDE_CONFIG` | `~/.config/telegram-claude-control.env` | Secret config file. |
| `TELEGRAM_CLAUDE_STATE` | `~/.local/state/telegram-claude-control.json` | Pairing state and Telegram update offset. |
| `TELEGRAM_CLAUDE_SESSION` | `claude` | tmux target session for the `/tmux` bridge. |
| `TELEGRAM_CLAUDE_WORKSPACE` | `$HOME` | Working directory for headless `claude -p` calls. |
| `TELEGRAM_CLAUDE_BIN` | `~/.local/bin/claude` | Path to the `claude` executable. |
| `TELEGRAM_CLAUDE_PERMISSION_MODE` | *(unset)* | Passed as `--permission-mode` to headless calls. Leave unset to inherit whatever `permissions.defaultMode` is configured in `~/.claude/settings.json`. |
| `TELEGRAM_CLAUDE_ASK_TIMEOUT` | `600` | Seconds to wait for a headless `claude -p` turn before giving up. |
| `TELEGRAM_CLAUDE_SH_TIMEOUT` | `60` | Seconds to wait for a `/sh` command before giving up. |
| `TELEGRAM_CLAUDE_BG_TIMEOUT` | `14400` | Seconds to wait for a `/bg` background job before giving up (4 hours). |
| `TELEGRAM_CLAUDE_DIFF_PREVIEW_LINES` | `10` | Inline the diff in an edited-file notification when total changed lines (added + removed) is below this; otherwise show only the counts. |

## Operational notes

- **Headless calls execute with real tool access.** `claude -p` cannot show
  interactive permission prompts (there's no TTY), so it needs either
  `permissions.defaultMode` set to something non-interactive (e.g.
  `bypassPermissions` or `acceptEdits`) in `~/.claude/settings.json`, or
  `TELEGRAM_CLAUDE_PERMISSION_MODE` set explicitly — otherwise turns that
  need a tool permission will fail rather than hang. Whatever mode is active,
  anyone who can message the paired chat can make Claude Code read, edit, and
  run commands on this host. Pair only a Telegram account you fully trust,
  and keep `BOT_TOKEN`/`PAIR_CODE` out of source control and logs.
- The bot uses long polling; do not run two controller instances with the
  same bot token, or they may consume each other's updates.
- The sender connection is warmed at startup and kept alive to minimize the
  delay before the acknowledgement reaction. Long polling uses a separate
  connection so it never blocks an acknowledgement or final reply.
- The state file is written with mode `0600` and records the paired chat ID
  (not the Claude session ID, which lives only in the running process's
  memory and resets on restart). Delete the state file to allow pairing a
  different chat.
- Outgoing Telegram text is capped at the API's 4096-character limit; tmux
  output is trimmed to recent lines for the same reason.

## Troubleshooting

- **Bot does not respond:** check process logs, the bot token, network
  access, and make sure no second polling process shares the same bot token.
- **`tmux session is unavailable`:** create the configured session (see step
  3 above) or set `TELEGRAM_CLAUDE_SESSION` to the actual session name.
- **`claude -p request failed`:** run the same `claude -p "..." --output-format json`
  command manually as the service user to see the raw error — usually a
  missing/expired login, an unreachable `TELEGRAM_CLAUDE_BIN` path, or a
  permission-mode mismatch (see the note above).

## Files

- `telegram-claude-control.py` — controller program.
- `telegram-claude-control.env.example` — safe secret-config template.
- `systemd/` — optional service template.
- `claude-code-remote-control.md` — research notes on Anthropic's own Remote
  Control feature and why this bot doesn't use it.

## License

[MIT](LICENSE).
