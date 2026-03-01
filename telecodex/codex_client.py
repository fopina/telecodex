from __future__ import annotations

import copy
import json
import shlex
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AskResult:
    reply: str
    unprocessed_messages: list[str]


@dataclass(slots=True)
class ModelOption:
    model_id: str
    display_name: str


def should_report_verbose_unhandled_message(msg: dict[str, Any]) -> bool:
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

    def _send(self, obj: dict[str, Any]) -> None:
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

    def _read_message(self) -> tuple[dict[str, Any], str]:
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
        if msg.get('method') != 'account/rateLimits/updated':
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
        if msg.get('method') != 'codex/event/token_count':
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

    def _request(
        self, method: str, params: dict[str, Any], unprocessed_messages: list[str] | None = None
    ) -> dict[str, Any]:
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

    def _notify(self, method: str, params: dict[str, Any]) -> None:
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
                        if error_message == 'Unknown app-server error' and turn_error_message:
                            error_message = turn_error_message
                        raise RuntimeError(error_message)
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
