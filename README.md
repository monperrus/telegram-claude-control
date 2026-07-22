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
| `/model` | Shows the model for this conversation and offers `sonnet`/`opus`/`haiku` as buttons. |
| `/model <name>` | Sets `--model <name>` for this conversation's `claude -p` calls (`/model default` clears it). |
| `/usage` | Current session/weekly usage against your plan's limits — free, instant, uses no tokens. `/help` also offers this as a button. |
| `/tmux <text>` | Types text and Enter into an interactive `claude` session running in the configured tmux window, then returns its recent output. |
| `/sh <command>` | Runs a shell command directly (not through tmux) and returns its combined stdout/stderr. |
| `/task <prompt>` | Runs `claude -p` headless in the background, not bound by the `/ask` timeout — a message with the result (or failure) lands here whenever the job actually finishes, even hours later. |
| `/jobs` | Lists the 8 most recent background jobs (icon, id, status, duration, prompt) — history survives a controller restart. |
| `/jobs <job_id>` | Detail view: status, duration, thread, full prompt, and result/error. |
| `/cancel <job_id>` | Kills a running background job. |
| `/restart` | Restarts the controller's systemd `--user` service (see Install). |
| `/screen` | Picks a tmux session/window/pane via inline buttons across the whole tmux server, skipping straight to content wherever there's only one choice. |
| `/screen_show <target>` | Captures and returns a specific tmux target directly (e.g. `web:1`, `web:1.0`) — what the `/screen` buttons call under the hood. |
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
your message acknowledges receipt immediately, before the turn finishes. A
"typing…" indicator also stays up for the whole `/ask` turn, refreshed on
each completed tool call, so a slow reply doesn't look like a dropped
message. Common `claude -p` failures (rate limits, an overloaded API) are
translated into a short, plain-English message instead of a raw error dump.

Commands are tappable two ways: `/help`'s reply carries inline buttons for
every command that needs no argument (`usage`, `status`, `screen`, `jobs`,
`model`, `interrupt`, `newsession`, `restart`), and the controller registers
the full command list with Telegram (`setMyCommands`) at startup, so typing
`/` in any client brings up the native autocomplete menu with descriptions
— tap one to insert `/command ` and just type the argument for commands
like `/ask`, `/task`, `/sh`, `/tmux`, `/cancel`, `/model <name>`.

One-letter shortcuts save typing on a phone: `h`=`/help` `s`=`/status`
`v`=`/screen` `i`=`/interrupt` `r`=`/restart` `t`=`/jobs` (bare) or `/task`
(`t <prompt>`) `a`=`/model` (bare) or `/model <name>` (`a <name>`) `u`=`/usage`
`m <text>`=`/tmux <text>` `x <cmd>`=`/sh <cmd>` `c <prompt>`=`/ask <prompt>`.
Only an exact `<letter>` or `<letter> <rest>` triggers a shortcut —
anything else (including sentences that happen to start with one of these
letters, e.g. "im on my way") falls through to the normal `/ask` prompt path
unchanged. The one exception is `x`/`m`/`t`/`c`/`a`: a message that
genuinely starts with one of those letters followed by a space (e.g. "x-ray"
typed as "x ray gun") will be misread as a shortcut — say it another way, or
use the full `/sh`/`/tmux`/`/task`/`/ask`/`/model` command instead.

`/usage` runs Claude Code's own built-in `/usage` command (aliased
`/cost`/`/stats`) directly via `claude -p "/usage" --output-format json` —
not through the tmux bridge, and not tied to any conversation's `--resume`
session. It's a free, local, near-instant query (no API tokens spent)
reporting current session and weekly usage against your plan's limits, plus
what's contributing to it.

The per-conversation model choice (`/model`) is persisted the same way the
`--resume` session id is — it survives a controller restart and is
independent of `/newsession`, which only clears the conversation history,
not the model.

The first chat must pair using a secret pairing code. Once paired, messages
from all other chats are silently ignored. In Telegram forum groups, each
topic (`message_thread_id`) gets its own independent `claude -p` session and
replies land back in that same topic, so parallel topics never share or
clobber each other's Claude conversation.

Only one request (foreground `/ask` or background `/task`) runs per topic at a
time, since two processes can't safely share the same `--resume` session —
a second request on the same topic is rejected with a "already running"
reply rather than queued. Different topics run fully in parallel, so a
long `/task` job in one topic never blocks `/ask` in another.

Every `/task` job is logged to a small SQLite database
(`TELEGRAM_CLAUDE_JOBS_DB`) as it starts and finishes, so `/jobs` history
survives a controller restart even though the underlying `claude -p`
subprocess itself can't: a job still marked "running" when the controller
starts up is a leftover from a previous process and gets flagged
"interrupted" rather than left looking falsely active. The conversation
itself isn't lost either way — it's still resumable normally via `--resume`
with a fresh `/ask` or `/task` on the same topic — only that specific job's
progress tracking ends at the interruption.

`/screen` walks the *entire* tmux server, not just `TELEGRAM_CLAUDE_SESSION`
— multiple sessions get a button per session, tapping one shows its windows,
tapping a window shows its panes, and tapping a pane captures it. Each level
is skipped automatically when there's only one choice, so a single-session,
single-window, single-pane setup (the common case) still returns content
immediately on a bare `/screen`, same as before. Button taps are Telegram
`callback_query` updates: the bot acknowledges them (so the client stops
showing a spinner) and replays the button's payload through the same command
dispatcher a typed message would use — a `/screen` button is, under the
hood, just `/screen_show <target>` sent on your behalf.

## Requirements

- Python 3.9+; standard library only, no dependencies to install.
- `tmux` on `PATH` (only needed for the `/tmux`, `/screen`, `/status`,
  `/interrupt` commands).
- `claude` (Claude Code CLI) installed and authenticated on this host.
- A Telegram bot token from [BotFather](https://t.me/BotFather).
- `systemd --user` (only needed for `install.sh` and `/restart`; everything
  else works fine run by hand or under another supervisor).

## Install

Quickest path — installs a `systemd --user` service running straight out of
this checkout (no copy elsewhere):

```sh
./install.sh
# or non-interactively:
BOT_TOKEN='your-token' ./install.sh
```

`install.sh` prompts for (or accepts via `--bot-token`/`BOT_TOKEN`) the bot
token, generates a `PAIR_CODE` if you don't pass one, writes
`~/.config/telegram-claude-control.env` at mode 600, and installs+enables
`systemd/telegram-claude-controller.user.service` under
`~/.config/systemd/user/`. Options: `--force` (regenerate the config),
`--no-start` (install/enable without starting). This is also what `/restart`
targets, so pairing and restarting from Telegram only work once the service
is installed this way (or an equivalent unit named `telegram-claude-controller.service`
exists — override the name via `TELEGRAM_CLAUDE_UNIT`).

Validate an install without entering the polling loop:

```sh
./telegram-claude-control.py --check
```

Checks config file permissions, `tmux` on `PATH`, the `claude` binary,
the workspace directory, tmux session availability (warning only), and
Telegram API connectivity.

For the bot to keep running after you log out of this machine:

```sh
loginctl enable-linger "$USER"
```

If you want the `/tmux` bridge, start an interactive Claude Code session in
a named tmux session (default name `claude`) before pairing:

```sh
tmux new-session -d -s claude "claude"
```

Then in Telegram, send `/pair <your pairing code>` from the one chat that
should control the bot. Use `/help` to confirm it's working.

### Manual install

Prefer to run it by hand (e.g. under your own process supervisor) instead of
`install.sh`:

1. Create a config file with restricted permissions:

   ```sh
   mkdir -p ~/.config ~/.local/state
   cp telegram-claude-control.env.example ~/.config/telegram-claude-control.env
   chmod 600 ~/.config/telegram-claude-control.env
   ```

2. Set `BOT_TOKEN` and a long, random `PAIR_CODE` in that file.
3. Run the controller manually:

   ```sh
   ./telegram-claude-control.py
   ```

For a system-wide (not `--user`) service, adapt
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
| `TELEGRAM_CLAUDE_JOBS_DB` | `~/.local/state/telegram-claude-control-jobs.db` | SQLite log of `/task` jobs, for `/jobs` history across restarts. |
| `TELEGRAM_CLAUDE_SESSION` | `claude` | tmux target session for the `/tmux` bridge. |
| `TELEGRAM_CLAUDE_WORKSPACE` | `$HOME` | Working directory for headless `claude -p` calls. |
| `TELEGRAM_CLAUDE_BIN` | `~/.local/bin/claude` | Path to the `claude` executable. |
| `TELEGRAM_CLAUDE_PERMISSION_MODE` | *(unset)* | Passed as `--permission-mode` to headless calls. Leave unset to inherit whatever `permissions.defaultMode` is configured in `~/.claude/settings.json`. |
| `TELEGRAM_CLAUDE_ASK_TIMEOUT` | `600` | Seconds to wait for a headless `claude -p` turn before giving up. |
| `TELEGRAM_CLAUDE_SH_TIMEOUT` | `60` | Seconds to wait for a `/sh` command before giving up. |
| `TELEGRAM_CLAUDE_TASK_TIMEOUT` | `14400` | Seconds to wait for a `/task` background job before giving up (4 hours). |
| `TELEGRAM_CLAUDE_USAGE_TIMEOUT` | `30` | Seconds to wait for `/usage` before giving up. |
| `TELEGRAM_CLAUDE_UNIT` | `telegram-claude-controller.service` | systemd `--user` unit name that `/restart` restarts. |
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
- The state file is written with mode `0600` and records the paired chat ID,
  each conversation's `--resume` session id, and each conversation's
  `/model` choice — all of which survive a controller restart. Delete the
  state file to allow pairing a different chat (this also forgets every
  conversation's session and model).
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
- **`/restart` replies "Restart failed: Unit ... not found":** the
  controller isn't running under the systemd unit `/restart` expects — run
  `./install.sh` (or set `TELEGRAM_CLAUDE_UNIT` to the actual unit name).

## Files

- `telegram-claude-control.py` — controller program (also: `--check`).
- `install.sh` — installs config + the systemd `--user` service.
- `telegram-claude-control.env.example` — safe secret-config template.
- `systemd/` — service templates (`.user.service` installed by `install.sh`;
  `.service.example` is a system-wide template to adapt by hand).
- `claude-code-remote-control.md` — research notes on Anthropic's own Remote
  Control feature and why this bot doesn't use it.

## License

[MIT](LICENSE).
