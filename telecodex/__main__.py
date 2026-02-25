#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from contextlib import suppress

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
POLL_TIMEOUT_SECONDS = int(os.getenv('POLL_TIMEOUT_SECONDS', '30'))

CODEX_APP_SERVER_CMD = os.getenv('CODEX_APP_SERVER_CMD', 'codex app-server')
CODEX_MODEL = os.getenv('CODEX_MODEL', 'gpt-5')
CODEX_CWD = os.getenv('CODEX_CWD', os.getcwd())
CODEX_APPROVAL_POLICY = os.getenv('CODEX_APPROVAL_POLICY', 'never')


class CodexStdioClient:
    def __init__(self, command: str) -> None:
        self.command = command
        self.proc: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.lock = threading.Lock()
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
                'cwd': CODEX_CWD,
                'model': CODEX_MODEL,
                'approvalPolicy': CODEX_APPROVAL_POLICY,
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

    def _read_message(self) -> dict:
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
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                return msg

    def _request(self, method: str, params: dict) -> dict:
        req_id = self.next_id
        self.next_id += 1

        self._send({'id': req_id, 'method': method, 'params': params})

        while True:
            msg = self._read_message()
            if msg.get('id') == req_id:
                if 'error' in msg:
                    raise RuntimeError(f'{method} failed: {msg["error"]}')
                return msg.get('result', {})

    def _notify(self, method: str, params: dict) -> None:
        self._send({'method': method, 'params': params})

    def ask(self, text: str) -> str:
        with self.lock:
            self._ensure_running()
            if not self.thread_id:
                raise RuntimeError('No thread initialized')

            turn_result = self._request(
                'turn/start',
                {
                    'threadId': self.thread_id,
                    'input': [{'type': 'text', 'text': text}],
                },
            )
            turn = turn_result.get('turn') if isinstance(turn_result, dict) else None
            turn_id = turn.get('id') if isinstance(turn, dict) else None
            if not turn_id:
                raise RuntimeError(f'turn/start did not return turn id: {turn_result}')

            chunks: list[str] = []
            fallback_final: str | None = None

            while True:
                msg = self._read_message()

                method = msg.get('method')
                params = msg.get('params')
                if not method or not isinstance(params, dict):
                    continue

                if method == 'item/agentMessage/delta' and params.get('turnId') == turn_id:
                    delta = params.get('delta')
                    if isinstance(delta, str):
                        chunks.append(delta)
                    continue

                if method == 'turn/completed':
                    completed_turn = params.get('turn')
                    completed_turn_id = completed_turn.get('id') if isinstance(completed_turn, dict) else None
                    if completed_turn_id != turn_id:
                        continue

                    agent_state = completed_turn.get('agentState') if isinstance(completed_turn, dict) else None
                    message = agent_state.get('message') if isinstance(agent_state, dict) else None
                    if isinstance(message, str) and message.strip():
                        fallback_final = message
                    break

            final = ''.join(chunks).strip()
            if final:
                return final
            if fallback_final:
                return fallback_final
            return 'No text response returned by app-server.'

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


def require_env() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print('Missing TELEGRAM_BOT_TOKEN', file=sys.stderr)
        sys.exit(1)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    text = (message.text or '').strip()
    if not text:
        return

    codex = context.application.bot_data['codex']
    assert isinstance(codex, CodexStdioClient)

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        reply = await asyncio.to_thread(codex.ask, text)
    except Exception as exc:  # noqa: BLE001
        reply = f'app-server error: {exc}'

    await message.reply_text(reply[:4096], reply_to_message_id=message.message_id)


async def handle_error(_: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f'Loop error: {context.error}', file=sys.stderr)


def main() -> None:
    require_env()

    codex = CodexStdioClient(CODEX_APP_SERVER_CMD)
    while True:
        try:
            codex.start()
            app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
            app.bot_data['codex'] = codex
            app.add_handler(MessageHandler(filters.TEXT, handle_message))
            app.add_error_handler(handle_error)

            print('Bot is running (Telegram <-> codex app-server over stdio).')
            app.run_polling(
                allowed_updates=['message'],
                timeout=POLL_TIMEOUT_SECONDS,
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


if __name__ == '__main__':
    main()
