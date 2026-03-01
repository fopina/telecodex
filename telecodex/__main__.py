#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import copy
import html
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import classyclick
import click
from platformdirs import user_config_dir
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    allowed_chat_id: int | None
    acp_log_file: str | None
    poll_timeout_seconds: int
    codex_app_server_cmd: str
    codex_model: str
    codex_cwd: str
    codex_approval_policy: str


@dataclass(slots=True)
class AskResult:
    reply: str
    unprocessed_messages: list[str]


@dataclass(slots=True)
class ModelOption:
    model_id: str
    display_name: str


DEFAULT_CONFIG_PATH = str(Path(user_config_dir('telecodex')) / 'config.toml')
CONFIG_SECTION = 'telecodex'
CONFIG_KEYS = {
    'telegram_bot_token',
    'allowed_chat_id',
    'acp_log_file',
    'poll_timeout_seconds',
    'codex_app_server_cmd',
    'codex_model',
    'codex_cwd',
    'codex_approval_policy',
}
PENDING_MODEL_INPUT_KEY = 'pending_model_input'


def load_settings_from_toml(config_path: str) -> dict[str, Any]:
    path = Path(config_path).expanduser()
    if not path.exists():
        return {}

    with path.open('rb') as fh:
        data = tomllib.load(fh)

    if not isinstance(data, dict):
        raise ValueError(f'Config file {path} must contain a TOML table at the root.')

    section = data.get(CONFIG_SECTION)
    if section is None:
        candidate = data
    elif isinstance(section, dict):
        candidate = section
    else:
        raise ValueError(f'[{CONFIG_SECTION}] in {path} must be a TOML table.')

    values: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        value = candidate.get(key)
        if value is not None:
            values[key] = value
    return values


def config_callback(ctx: click.Context, _: click.Parameter, value: str) -> str:
    try:
        config_values = load_settings_from_toml(value)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise click.BadParameter(str(exc)) from exc
    if config_values:
        existing = dict(ctx.default_map or {})
        existing.update(config_values)
        ctx.default_map = existing
    return value


class CodexStdioClient:
    def __init__(self, command: str, model: str, cwd: str, approval_policy: str, acp_log_file: str | None) -> None:
        self.command = command
        self.model = model
        self.cwd = cwd
        self.approval_policy = approval_policy
        self.acp_log_file = Path(acp_log_file).expanduser() if acp_log_file else None
        self.proc: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.lock = threading.Lock()
        self.acp_log_lock = threading.Lock()
        self.rate_limits_lock = threading.Lock()
        self.rate_limits_by_id: dict[Any, dict[str, Any]] = {}
        self.token_usage_lock = threading.Lock()
        self.latest_token_usage: dict[str, Any] | None = None
        self.thread_id: str | None = None

    def start(self) -> None:
        argv = shlex.split(self.command)
        if not argv:
            raise RuntimeError('CODEX_APP_SERVER_CMD is empty')

        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._ensure_log_file()

        self._request(
            'initialize',
            {
                'clientInfo': {
                    'name': 'telegram-codex-bot',
                    'version': '0.1.0',
                },
            },
        )
        self._notify('initialized', {})

        start_result = self._request(
            'thread/start',
            {
                'cwd': self.cwd,
                'model': self.model,
                'approvalPolicy': self.approval_policy,
            },
        )

        thread = start_result.get('thread') if isinstance(start_result, dict) else None
        thread_id = thread.get('id') if isinstance(thread, dict) else None
        if not thread_id:
            raise RuntimeError(f'thread/start did not return thread id: {start_result}')
        self.thread_id = thread_id

    def _ensure_running(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            err = ''
            if self.proc and self.proc.stderr:
                try:
                    err = self.proc.stderr.read()
                except Exception:
                    err = ''
            raise RuntimeError(f'app-server not running. stderr: {err[:2000]}')

    def _send(self, obj: dict) -> None:
        self._ensure_running()
        assert self.proc is not None and self.proc.stdin is not None
        line = json.dumps(obj, ensure_ascii=False)
        self.proc.stdin.write(line + '\n')
        self.proc.stdin.flush()

    def _ensure_log_file(self) -> None:
        if self.acp_log_file is None:
            return
        self.acp_log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.acp_log_file.open('a', encoding='utf-8'):
            pass

    def _log_acp_message(self, line: str) -> None:
        if self.acp_log_file is None:
            return
        with self.acp_log_lock:
            with self.acp_log_file.open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')

    def _read_message(self) -> tuple[dict, str]:
        self._ensure_running()
        assert self.proc is not None and self.proc.stdout is not None

        while True:
            line = self.proc.stdout.readline()
            if line == '':
                self._ensure_running()
                raise RuntimeError('Unexpected EOF from app-server stdout')
            line = line.strip()
            if not line:
                continue
            self._log_acp_message(line)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self._track_rate_limits(msg)
                self._track_token_usage(msg)
                return msg, line

    def _track_rate_limits(self, msg: dict[str, Any]) -> None:
        method = msg.get('method')
        if method != 'account/rateLimits/updated':
            return
        params = msg.get('params')
        if not isinstance(params, dict):
            return
        rate_limits = params.get('rateLimits')
        if not isinstance(rate_limits, dict):
            return
        limit_id = rate_limits.get('limitId')
        with self.rate_limits_lock:
            self.rate_limits_by_id[limit_id] = copy.deepcopy(rate_limits)

    def get_rate_limits_snapshot(self) -> dict[Any, dict[str, Any]]:
        with self.rate_limits_lock:
            return copy.deepcopy(self.rate_limits_by_id)

    def read_rate_limits(self) -> dict[Any, dict[str, Any]]:
        with self.lock:
            result = self._request('account/rateLimits/read', {})

        by_limit_id = result.get('rateLimitsByLimitId') if isinstance(result, dict) else None
        normalized: dict[Any, dict[str, Any]] = {}
        if isinstance(by_limit_id, dict):
            for key, value in by_limit_id.items():
                if isinstance(value, dict):
                    normalized[key] = copy.deepcopy(value)
        else:
            single = result.get('rateLimits') if isinstance(result, dict) else None
            if isinstance(single, dict):
                normalized[single.get('limitId')] = copy.deepcopy(single)

        if normalized:
            with self.rate_limits_lock:
                self.rate_limits_by_id = copy.deepcopy(normalized)
            return normalized

        return self.get_rate_limits_snapshot()

    def _track_token_usage(self, msg: dict[str, Any]) -> None:
        method = msg.get('method')
        if method != 'codex/event/token_count':
            return
        params = msg.get('params')
        if not isinstance(params, dict):
            return
        nested_msg = params.get('msg')
        if not isinstance(nested_msg, dict):
            return
        info = nested_msg.get('info')
        if not isinstance(info, dict):
            return
        with self.token_usage_lock:
            self.latest_token_usage = copy.deepcopy(info)

    def get_latest_token_usage(self) -> dict[str, Any] | None:
        with self.token_usage_lock:
            return copy.deepcopy(self.latest_token_usage)

    def get_model(self) -> str:
        with self.lock:
            return self.model

    def set_model(self, model: str) -> None:
        with self.lock:
            self.model = model

    def list_models(self) -> list[ModelOption]:
        with self.lock:
            result = self._request('model/list', {})

        data = result.get('data') if isinstance(result, dict) else None
        if not isinstance(data, list):
            return []

        models: list[ModelOption] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            raw_model_id = item.get('model') or item.get('id')
            if not isinstance(raw_model_id, str) or not raw_model_id:
                continue
            raw_display_name = item.get('displayName')
            display_name = raw_display_name if isinstance(raw_display_name, str) and raw_display_name else raw_model_id
            models.append(ModelOption(model_id=raw_model_id, display_name=display_name))
        return models

    def _request(self, method: str, params: dict, unprocessed_messages: list[str] | None = None) -> dict:
        req_id = self.next_id
        self.next_id += 1

        self._send({'id': req_id, 'method': method, 'params': params})

        while True:
            msg, raw_message = self._read_message()
            if msg.get('id') == req_id:
                if 'error' in msg:
                    raise RuntimeError(f'{method} failed: {msg["error"]}')
                return msg.get('result', {})
            if unprocessed_messages is not None and should_report_verbose_unhandled_message(msg):
                unprocessed_messages.append(raw_message)

    def _notify(self, method: str, params: dict) -> None:
        self._send({'method': method, 'params': params})

    def ask(self, text: str) -> AskResult:
        with self.lock:
            self._ensure_running()
            if not self.thread_id:
                raise RuntimeError('No thread initialized')

            unprocessed_messages: list[str] = []
            turn_result = self._request(
                'turn/start',
                {
                    'threadId': self.thread_id,
                    'input': [{'type': 'text', 'text': text}],
                    'model': self.model,
                },
                unprocessed_messages=unprocessed_messages,
            )
            turn = turn_result.get('turn') if isinstance(turn_result, dict) else None
            turn_id = turn.get('id') if isinstance(turn, dict) else None
            if not turn_id:
                raise RuntimeError(f'turn/start did not return turn id: {turn_result}')

            chunks: list[str] = []
            fallback_final: str | None = None
            turn_error_message: str | None = None

            while True:
                msg, raw_message = self._read_message()

                method = msg.get('method')
                params = msg.get('params')
                if not method or not isinstance(params, dict):
                    unprocessed_messages.append(raw_message)
                    continue

                if method == 'item/agentMessage/delta' and params.get('turnId') == turn_id:
                    delta = params.get('delta')
                    if isinstance(delta, str):
                        chunks.append(delta)
                    continue

                if method == 'error' and params.get('turnId') == turn_id:
                    error_message = extract_error_message(params.get('error'))
                    turn_error_message = error_message
                    if params.get('willRetry') is False:
                        raise RuntimeError(error_message)
                    continue

                if method == 'turn/completed':
                    completed_turn = params.get('turn')
                    completed_turn_id = completed_turn.get('id') if isinstance(completed_turn, dict) else None
                    if completed_turn_id != turn_id:
                        if should_report_verbose_unhandled_message(msg):
                            unprocessed_messages.append(raw_message)
                        continue

                    agent_state = completed_turn.get('agentState') if isinstance(completed_turn, dict) else None
                    message = agent_state.get('message') if isinstance(agent_state, dict) else None
                    if isinstance(message, str) and message.strip():
                        fallback_final = message
                    if completed_turn.get('status') == 'failed':
                        error_message = extract_error_message(completed_turn.get('error'))
                        raise RuntimeError(
                            error_message
                            if error_message != 'Unknown app-server error'
                            else turn_error_message or error_message
                        )
                    break
                if should_report_verbose_unhandled_message(msg):
                    unprocessed_messages.append(raw_message)

            final = ''.join(chunks).strip()
            if final:
                return AskResult(reply=final, unprocessed_messages=unprocessed_messages)
            if fallback_final:
                return AskResult(reply=fallback_final, unprocessed_messages=unprocessed_messages)
            return AskResult(
                reply='No text response returned by app-server.', unprocessed_messages=unprocessed_messages
            )

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            return

        self.proc.terminate()
        with suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=5)

        if self.proc.poll() is None:
            self.proc.kill()
            with suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)


def require_env(settings: Settings) -> None:
    if not settings.telegram_bot_token:
        print('Missing TELEGRAM_BOT_TOKEN', file=sys.stderr)
        sys.exit(1)
    if settings.allowed_chat_id is None:
        print(
            'Missing allowed chat id (--allowed-chat-id / TELEGRAM_ALLOWED_CHAT_ID / config allowed_chat_id)',
            file=sys.stderr,
        )
        sys.exit(1)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    text = (message.text or '').strip()
    if not text:
        return

    allowed_chat_id = context.application.bot_data.get('allowed_chat_id')
    if message.chat_id != allowed_chat_id:
        return

    if context.application.bot_data.get(PENDING_MODEL_INPUT_KEY):
        codex = context.application.bot_data['codex']
        assert isinstance(codex, CodexStdioClient)
        await asyncio.to_thread(codex.set_model, text)
        context.application.bot_data[PENDING_MODEL_INPUT_KEY] = False
        await reply_markdown(
            message,
            f'Model updated to `{text}` for next turns.',
            reply_to_message_id=message.message_id,
        )
        return

    await process_user_input(message, context, text)


def format_raw_json_markdown(raw_message: str) -> str:
    safe_raw = raw_message.replace('```', '``\\`')
    return f'```json\n{safe_raw}\n```'


def format_raw_json_expandable_blockquote(raw_message: str) -> str:
    escaped = html.escape(raw_message)
    return f'<blockquote expandable>{escaped}</blockquote>'


def is_delta_message(raw_message: str) -> bool:
    try:
        msg = json.loads(raw_message)
    except json.JSONDecodeError:
        return False
    if not isinstance(msg, dict):
        return False

    method = msg.get('method')
    if isinstance(method, str) and 'delta' in method.lower():
        return True

    params = msg.get('params')
    if isinstance(params, dict):
        nested_msg = params.get('msg')
        if isinstance(nested_msg, dict):
            msg_type = nested_msg.get('type')
            if isinstance(msg_type, str) and 'delta' in msg_type.lower():
                return True

    return False


def should_report_verbose_unhandled_message(msg: dict) -> bool:
    method = msg.get('method')
    if not isinstance(method, str):
        return True
    if 'delta' in method.lower():
        return False
    if method in {
        'item/agentMessage/delta',
        'turn/completed',
        'account/rateLimits/updated',
        'codex/event/token_count',
        'thread/tokenUsage/updated',
    }:
        return False
    return True


def extract_error_message(error_payload: Any) -> str:
    if not isinstance(error_payload, dict):
        return str(error_payload) if error_payload is not None else 'Unknown app-server error'

    raw_message = error_payload.get('message')
    if isinstance(raw_message, str) and raw_message.strip():
        with suppress(json.JSONDecodeError, TypeError):
            parsed = json.loads(raw_message)
            detail = parsed.get('detail') if isinstance(parsed, dict) else None
            if isinstance(detail, str) and detail.strip():
                return detail
        return raw_message

    codex_error_info = error_payload.get('codexErrorInfo')
    if isinstance(codex_error_info, str) and codex_error_info:
        return codex_error_info
    return 'Unknown app-server error'


async def reply_markdown(message: Any, text: str, reply_to_message_id: int) -> None:
    text = text[:4096]
    try:
        await message.reply_text(
            text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except BadRequest:
        await message.reply_text(
            text,
            reply_to_message_id=reply_to_message_id,
            disable_web_page_preview=True,
        )


async def reply_expandable_blockquote(message: Any, text: str, reply_to_message_id: int) -> None:
    max_payload = 4000
    payload = text[:max_payload]
    try:
        await message.reply_text(
            format_raw_json_expandable_blockquote(payload),
            reply_to_message_id=reply_to_message_id,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest:
        await reply_markdown(
            message,
            format_raw_json_markdown(payload),
            reply_to_message_id=reply_to_message_id,
        )


async def process_user_input(message: Any, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    codex = context.application.bot_data['codex']
    assert isinstance(codex, CodexStdioClient)

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        ask_result = await asyncio.to_thread(codex.ask, text)
        assert isinstance(ask_result, AskResult)
    except Exception as exc:  # noqa: BLE001
        await reply_markdown(message, f'app-server error: {exc}', reply_to_message_id=message.message_id)
        return

    await reply_markdown(message, ask_result.reply, reply_to_message_id=message.message_id)

    if context.application.bot_data.get('verbose'):
        for raw_message in ask_result.unprocessed_messages:
            if is_delta_message(raw_message):
                continue
            await reply_expandable_blockquote(message, raw_message, reply_to_message_id=message.message_id)


async def handle_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    allowed_chat_id = context.application.bot_data.get('allowed_chat_id')
    if message.chat_id != allowed_chat_id:
        return
    await process_user_input(message, context, 'hello')


async def handle_verbose_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    allowed_chat_id = context.application.bot_data.get('allowed_chat_id')
    if message.chat_id != allowed_chat_id:
        return
    verbose = bool(context.application.bot_data.get('verbose'))
    verbose = not verbose
    context.application.bot_data['verbose'] = verbose
    status = 'ON' if verbose else 'OFF'
    await reply_markdown(message, f'Verbose mode is now `{status}`.', reply_to_message_id=message.message_id)


async def handle_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    allowed_chat_id = context.application.bot_data.get('allowed_chat_id')
    if message.chat_id != allowed_chat_id:
        return

    codex = context.application.bot_data['codex']
    assert isinstance(codex, CodexStdioClient)
    snapshot = await asyncio.to_thread(codex.read_rate_limits)
    token_usage = await asyncio.to_thread(codex.get_latest_token_usage)
    if not snapshot and not token_usage:
        await reply_markdown(
            message,
            'No rate limits or token usage received yet.',
            reply_to_message_id=message.message_id,
        )
        return

    lines: list[str] = []
    if snapshot:
        sorted_limits = sorted(snapshot.items(), key=lambda item: str(item[0]))
        visible_limits = [(limit_id, values) for limit_id, values in sorted_limits if should_render_rate_limit(values)]
        hidden_limit_names = [
            format_limit_name(limit_id) for limit_id, values in sorted_limits if not should_render_rate_limit(values)
        ]
        if visible_limits or hidden_limit_names:
            lines.append('')
            lines.append('*Rate Limits*')
            for limit_id, values in visible_limits:
                model = format_limit_name(limit_id)
                lines.append('')
                lines.append(f'*Limit:* `{model}`')

                primary = values.get('primary')
                secondary = values.get('secondary')
                lines.append(f'*Primary:* {format_rate_limit_bucket(primary)}')
                lines.append(f'*Secondary:* {format_rate_limit_bucket(secondary)}')
            if hidden_limit_names:
                lines.append('')
                lines.append(f'*Unused limits:* `{", ".join(hidden_limit_names)}`')

    if token_usage:
        lines.append('')
        lines.append('*Token Usage*')
        total = token_usage.get('total_token_usage')
        last = token_usage.get('last_token_usage')
        model_context_window = token_usage.get('model_context_window')
        lines.append(f'*Total:* {format_token_usage(total)}')
        lines.append(f'*Last:* {format_token_usage(last)}')
        lines.append(f'*Model Context Window:* `{model_context_window}`')

    await reply_markdown(message, '\n'.join(lines), reply_to_message_id=message.message_id)


def format_rate_limit_bucket(bucket: Any) -> str:
    if not isinstance(bucket, dict):
        return 'n/a'
    used_percent = bucket.get('usedPercent')
    resets_at = bucket.get('resetsAt')
    used_percent_display = f'{used_percent}%' if isinstance(used_percent, (int, float)) else 'n/a'
    reset_display = format_utc_timestamp(resets_at)
    return f'{used_percent_display} - {reset_display}'


def should_render_rate_limit(values: Any) -> bool:
    if not isinstance(values, dict):
        return True
    primary = values.get('primary')
    secondary = values.get('secondary')
    primary_used = primary.get('usedPercent') if isinstance(primary, dict) else None
    secondary_used = secondary.get('usedPercent') if isinstance(secondary, dict) else None
    return not (primary_used == 0 and secondary_used == 0)


def format_limit_name(limit_id: Any) -> str:
    return 'Global' if limit_id is None else str(limit_id)


def format_token_usage(usage: Any) -> str:
    if not isinstance(usage, dict):
        return 'n/a'
    total_tokens = usage.get('total_tokens')
    input_tokens = usage.get('input_tokens')
    output_tokens = usage.get('output_tokens')
    return f'total=`{total_tokens}` input=`{input_tokens}` output=`{output_tokens}`'


def format_utc_timestamp(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return 'n/a'
    dt = datetime.fromtimestamp(value, tz=timezone.utc)
    return dt.strftime('%Y-%m-%d %H:%M:%S UTC')


def build_model_menu(models: list[ModelOption], selected_model: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for model in models:
        marker = '✅ ' if model.model_id == selected_model else ''
        label = f'{marker}{model.display_name}'
        rows.append([InlineKeyboardButton(text=label, callback_data=f'model:set:{model.model_id}')])
    rows.append([InlineKeyboardButton(text='Free text', callback_data='model:free_text')])
    rows.append([InlineKeyboardButton(text='Cancel', callback_data='model:cancel')])
    return InlineKeyboardMarkup(rows)


async def setup_bot_commands(application: Any) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand(command='start', description='Start conversation with Codex'),
            BotCommand(command='verbose', description='Toggle verbose ACP debug messages'),
            BotCommand(command='status', description='Show latest ACP rate-limit updates'),
            BotCommand(command='model', description='Choose the Codex model'),
        ]
    )


async def handle_error(_: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f'Loop error: {context.error}', file=sys.stderr)


async def handle_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    allowed_chat_id = context.application.bot_data.get('allowed_chat_id')
    if message.chat_id != allowed_chat_id:
        return

    codex = context.application.bot_data['codex']
    assert isinstance(codex, CodexStdioClient)

    try:
        models = await asyncio.to_thread(codex.list_models)
        selected_model = await asyncio.to_thread(codex.get_model)
    except Exception as exc:  # noqa: BLE001
        await reply_markdown(message, f'app-server error: {exc}', reply_to_message_id=message.message_id)
        return

    if not models:
        await reply_markdown(message, 'No models available from app-server.', reply_to_message_id=message.message_id)
        return

    keyboard = build_model_menu(models, selected_model=selected_model)
    await message.reply_text(
        f'Select model for next turns (current: `{selected_model}`):',
        reply_to_message_id=message.message_id,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def handle_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    message = query.message
    if message is None:
        await query.answer()
        return

    allowed_chat_id = context.application.bot_data.get('allowed_chat_id')
    if message.chat_id != allowed_chat_id:
        await query.answer()
        return

    data = query.data or ''
    if data == 'model:cancel':
        context.application.bot_data[PENDING_MODEL_INPUT_KEY] = False
        await query.answer('Canceled')
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if data == 'model:free_text':
        context.application.bot_data[PENDING_MODEL_INPUT_KEY] = True
        await query.answer()
        await query.edit_message_text('Send the model id as a text message. It will be used for next turns.')
        return

    if not data.startswith('model:set:'):
        await query.answer()
        return

    model_id = data.removeprefix('model:set:')
    codex = context.application.bot_data['codex']
    assert isinstance(codex, CodexStdioClient)

    try:
        models = await asyncio.to_thread(codex.list_models)
    except Exception as exc:  # noqa: BLE001
        await query.answer('Failed to load models')
        await query.edit_message_text(f'Could not load models: {exc}')
        return

    available_ids = {model.model_id for model in models}
    if model_id not in available_ids:
        await query.answer('Model unavailable')
        selected_model = await asyncio.to_thread(codex.get_model)
        await query.edit_message_text(
            f'Model `{model_id}` is not available. Current model remains `{selected_model}`.',
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await asyncio.to_thread(codex.set_model, model_id)
    context.application.bot_data[PENDING_MODEL_INPUT_KEY] = False
    await query.answer(f'Model set to {model_id}')
    await query.edit_message_text(f'Model updated to `{model_id}` for next turns.', parse_mode=ParseMode.MARKDOWN)


def run_bot(settings: Settings) -> None:
    require_env(settings)

    codex = CodexStdioClient(
        settings.codex_app_server_cmd,
        settings.codex_model,
        settings.codex_cwd,
        settings.codex_approval_policy,
        settings.acp_log_file,
    )
    while True:
        try:
            codex.start()
            app = ApplicationBuilder().token(settings.telegram_bot_token).post_init(setup_bot_commands).build()
            app.bot_data['codex'] = codex
            app.bot_data['allowed_chat_id'] = settings.allowed_chat_id
            app.bot_data['verbose'] = False
            app.bot_data[PENDING_MODEL_INPUT_KEY] = False
            app.add_handler(CommandHandler('start', handle_start_command))
            app.add_handler(CommandHandler('verbose', handle_verbose_command))
            app.add_handler(CommandHandler('status', handle_status_command))
            app.add_handler(CommandHandler('model', handle_model_command))
            app.add_handler(CallbackQueryHandler(handle_model_callback, pattern=r'^model:(set:|free_text$|cancel$)'))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.add_error_handler(handle_error)

            print('Bot is running (Telegram <-> codex app-server over stdio).')
            app.run_polling(
                allowed_updates=['message', 'callback_query'],
                timeout=settings.poll_timeout_seconds,
                close_loop=False,
            )
            return
        except KeyboardInterrupt:
            print('Stopped by user')
            return
        except Exception as exc:  # noqa: BLE001
            print(f'Loop error: {exc}', file=sys.stderr)
            time.sleep(3)
        finally:
            codex.stop()


@classyclick.command()
class Telecodex:
    """Telegram bot bridge for codex app-server over stdio."""

    config: str = classyclick.Option(
        '--config',
        default=DEFAULT_CONFIG_PATH,
        type=str,
        show_default=True,
        is_eager=True,
        expose_value=False,
        callback=config_callback,
        help='TOML config file path (uses [telecodex] table or top-level keys).',
    )
    telegram_bot_token: str = classyclick.Option(
        envvar='TELEGRAM_BOT_TOKEN',
        default='',
        type=str,
        show_envvar=True,
        help='Telegram bot token.',
    )
    allowed_chat_id: int | None = classyclick.Option(
        envvar='TELEGRAM_ALLOWED_CHAT_ID',
        default=None,
        type=int,
        show_envvar=True,
        help='Only this Telegram chat id will receive replies.',
    )
    acp_log_file: str | None = classyclick.Option(
        envvar='TELECODEX_ACP_LOG_FILE',
        default=None,
        type=str,
        show_envvar=True,
        help='File path to append every ACP/app-server message received (disabled by default).',
    )
    poll_timeout_seconds: int = classyclick.Option(
        envvar='POLL_TIMEOUT_SECONDS',
        default=30,
        type=int,
        show_envvar=True,
        help='Telegram polling timeout in seconds.',
    )
    codex_app_server_cmd: str = classyclick.Option(
        envvar='CODEX_APP_SERVER_CMD',
        default='codex app-server',
        type=str,
        show_envvar=True,
        help='Command used to launch codex app-server.',
    )
    codex_model: str = classyclick.Option(
        envvar='CODEX_MODEL',
        default='gpt-5',
        type=str,
        show_envvar=True,
        help='Codex model passed to thread/start.',
    )
    codex_cwd: str = classyclick.Option(
        envvar='CODEX_CWD',
        default=os.getcwd(),
        type=str,
        show_envvar=True,
        help='Working directory for the codex app-server thread.',
    )
    codex_approval_policy: str = classyclick.Option(
        envvar='CODEX_APPROVAL_POLICY',
        default='never',
        type=str,
        show_envvar=True,
        help='Approval policy passed to thread/start.',
    )

    def __call__(self) -> None:
        settings = Settings(
            telegram_bot_token=self.telegram_bot_token,
            allowed_chat_id=self.allowed_chat_id,
            acp_log_file=self.acp_log_file,
            poll_timeout_seconds=self.poll_timeout_seconds,
            codex_app_server_cmd=self.codex_app_server_cmd,
            codex_model=self.codex_model,
            codex_cwd=self.codex_cwd,
            codex_approval_policy=self.codex_approval_policy,
        )
        run_bot(settings)


def main() -> None:
    Telecodex.click()


if __name__ == '__main__':
    main()
