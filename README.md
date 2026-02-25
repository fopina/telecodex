# telecodex

[![ci](https://github.com/fopina/telecodex/actions/workflows/publish-main.yml/badge.svg)](https://github.com/fopina/telecodex/actions/workflows/publish-main.yml)
[![test](https://github.com/fopina/telecodex/actions/workflows/test.yml/badge.svg)](https://github.com/fopina/telecodex/actions/workflows/test.yml)
[![codecov](https://codecov.io/github/fopina/telecodex/graph/badge.svg)](https://codecov.io/github/fopina/telecodex)
[![PyPI pyversions](https://img.shields.io/pypi/pyversions/telecodex.svg)](https://pypi.org/project/telecodex/)
[![Current version on PyPI](https://img.shields.io/pypi/v/telecodex)](https://pypi.org/project/telecodex/)

Telegram bot bridge for `codex app-server` over stdio JSON-RPC.

The bot receives Telegram messages, sends them to `codex app-server`, and replies back to Telegram chats.

## Requirements

- Python 3.10+
- A Telegram bot token (from `@BotFather`)
- `codex` CLI installed with `codex app-server` available

## Install

```bash
uv sync --group dev
```

## Environment variables

- `TELEGRAM_BOT_TOKEN` (required): Telegram bot token
- `CODEX_APP_SERVER_CMD` (optional): command to launch app-server. Default: `codex app-server`
- `CODEX_MODEL` (optional): model for `thread/start`. Default: `gpt-5`
- `CODEX_CWD` (optional): working directory for the app-server thread. Default: current working directory
- `CODEX_APPROVAL_POLICY` (optional): `untrusted|on-failure|on-request|never`. Default: `never`
- `POLL_TIMEOUT_SECONDS` (optional): Telegram long-poll timeout. Default: `30`

## Run

```bash
export TELEGRAM_BOT_TOKEN='123456:ABC...'
export CODEX_APP_SERVER_CMD='codex app-server'
export CODEX_MODEL='gpt-5'
python3 -m telecodex
```

## Development

- Lint/format: `make lint`
- Lint check only: `make lint-check`
- Tests: `make test`

## Protocol flow

The bot uses the `codex app-server` JSON-RPC flow:

1. `initialize`
2. `initialized` (notification)
3. `thread/start`
4. `turn/start` for each Telegram message
5. stream `item/agentMessage/delta`
6. wait for `turn/completed`
