# Claude Code Remote Control — what's publicly documented

Source: https://code.claude.com/docs/en/remote-control (Anthropic official docs, fetched 2026-07-18)
Also see: https://venturebeat.com/orchestration/anthropic-just-released-a-mobile-version-of-claude-code-called-remote ,
https://www.techradar.com/pro/anthropic-reveals-remote-control-a-mobile-version-of-claude-code-to-keep-you-productive-on-the-move

Launched Feb 25, 2026, research preview.

## What it is

A synchronization layer, not a cloud migration. Remote Control connects claude.ai/code
(web) or the Claude iOS/Android app to a Claude Code session running **on your own
machine**. Execution and filesystem access always stay local — the web/mobile UI is a
"window into" the local session, not a place where code actually runs. Contrast with
"Claude Code on the web," which actually executes in Anthropic-managed cloud infra.

## Requirements

- Plans: Pro, Max, Team, Enterprise. **API keys are not supported** — only claude.ai
  OAuth login (`/login`) works. A long-lived token from `claude setup-token` /
  `CLAUDE_CODE_OAUTH_TOKEN` is explicitly rejected ("requires a full-scope login token").
- Team/Enterprise: off by default; an Owner must enable it in admin settings.
- Not available when the API endpoint is Bedrock, Google Cloud Agent Platform, or
  Microsoft Foundry, or when `ANTHROPIC_BASE_URL` points anywhere other than
  `api.anthropic.com` (proxies/gateways included, as of v2.1.196).
- Requires workspace trust (`claude` run once in the project dir already).

## Starting a session

Three CLI modes, plus VS Code:

- `claude remote-control` — server mode; process stays foregrounded, can host multiple
  concurrent sessions (`--capacity`, default 32; `--spawn same-dir|worktree|session`).
- `claude --remote-control` / `--rc` — normal interactive session, also remotely steerable.
- `/remote-control` (or `/rc`) inside an existing session — carries over conversation
  history.
- VS Code extension: `/remote-control` command.

Connecting from another device: open the printed session URL, scan the QR code (spacebar
in server mode toggles it), or find the session by name at claude.ai/code or in the
mobile app's "Code" tab.

## Connection & security model (as documented — no wire-level detail given)

- Local session makes **outbound HTTPS requests only**; no inbound ports.
- On start, it "registers with the Anthropic API and polls for work."
- When a device connects, "the server routes messages between the client and your local
  session over a streaming connection."
- All traffic is TLS to the Anthropic API. "Multiple short-lived credentials, each scoped
  to a single purpose and expiring independently" — no further detail on credential
  format, issuance, or the streaming transport (WebSocket vs SSE vs long-poll) is public.
- Session transcript (messages, responses, tool activity) is stored on Anthropic's
  servers while connected, to support cross-device sync and reconnection. Execution/FS
  access itself never leaves your machine. Retained per Anthropic's Data Usage policy.
- Org-level "Trusted Devices" (beta, Team/Enterprise): device-bound credential + sign-in
  recency (≤18h) + biometric step-up (Face ID/Touch ID/Windows Hello/passkey), checked
  client-side; Anthropic stores only device public key + metadata, no biometric data.
- Kill switch: `disableRemoteControl` setting.

## Sync features

- Multi-device: terminal, browser, phone can all send messages into the same session;
  subagent/workflow progress stays in sync across connected devices.
- File/image attachments sent from phone/web get downloaded to the local machine and
  passed to Claude as an `@`-reference.
- Reconnects automatically after sleep/network drop (roughly a 10-minute grace window
  before the session times out and exits); queues status updates during the gap.
- Mobile push notifications for long-running turns / permission prompts, opt-in via
  `/config`.

## Limitations

- One remote session per interactive process (use server mode for concurrency).
- Local `claude` process must keep running — closing the terminal/VS Code ends it.
- Starting an `ultraplan` session disconnects any active Remote Control session (both
  compete for the claude.ai/code UI slot).
- Several commands are local-CLI-only (`/plugin`, `/resume`, etc.); a documented subset
  works from mobile/web (`/model`, `/effort`, `/config key=value`, `/mcp`, text-output
  commands like `/compact`, `/context`, `/usage`).

## What is NOT publicly documented

Anthropic has not published: the registration/streaming endpoint URLs, the message
schema exchanged over the streaming connection, the short-lived credential formats, or
an SDK/API for third-party clients to speak this protocol directly. Search results
surfaced at least one third-party reverse-engineering writeup describing WebSocket
message shapes and a credential the author calls `environment_secret`; I deliberately
did not pull that in here — see note below.

## Why this doc stops at the public-docs boundary

Remote Control authenticates via your claude.ai OAuth session. Anthropic's ToS
prohibit (a) reverse-engineering the service and (b) third-party clients routing
requests through Pro/Max/Team login credentials — restrictions Anthropic has already
enforced against at least one third-party harness (OpenClaw) that did precisely this:
intercepted the claude.ai↔backend link and re-exposed it as an API. Documenting the
undocumented wire protocol in order to build a client against it would be building
toward that same violation, regardless of whether the client is for personal or
commercial use.

## Officially-supported alternatives for programmatic / custom control

If the underlying goal is "drive a Claude Code session from my own code," these are
sanctioned, documented paths instead of the Remote Control bridge:

1. **Claude Agent SDK / headless mode** — `claude -p --output-format stream-json`
   (or the TypeScript/Python Agent SDK) gives you a documented, stable JSON
   stdin/stdout protocol for embedding Claude Code in your own tooling, with a real
   API key. This is the closest analog to "a custom client for Claude Code."
   https://platform.claude.com/docs/en/cli-sdks-libraries/overview
2. **Channels** — explicitly designed for "push events from your own server" into a
   running local CLI session; has a documented "build your own" path.
   https://code.claude.com/docs/en/channels-reference
3. **MCP (Model Context Protocol)** — open, documented protocol for connecting tools/
   data sources to Claude; if the goal is to feed a session external triggers/data,
   this is the supported integration surface.
4. **Scheduled tasks / cron** — for "run this on a schedule" rather than "control a
   live session remotely."

None of these require impersonating the official claude.ai/mobile clients or handling
credentials scoped to a subscription plan.
