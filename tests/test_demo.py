import tempfile
import unittest
from pathlib import Path

from telecodex.__main__ import load_settings_from_toml


class TestLoadSettingsFromToml(unittest.TestCase):
    def test_reads_telecodex_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / 'config.toml'
            config.write_text(
                '\n'.join(
                    [
                        '[telecodex]',
                        'telegram_bot_token = "token"',
                        'allowed_chat_id = 1234567890',
                        'acp_log_file = "/tmp/acp.log"',
                        'poll_timeout_seconds = 45',
                        'codex_model = "gpt-5"',
                    ]
                )
                + '\n',
                encoding='utf-8',
            )

            values = load_settings_from_toml(str(config))

            self.assertEqual(values['telegram_bot_token'], 'token')
            self.assertEqual(values['allowed_chat_id'], 1234567890)
            self.assertEqual(values['acp_log_file'], '/tmp/acp.log')
            self.assertEqual(values['poll_timeout_seconds'], 45)
            self.assertEqual(values['codex_model'], 'gpt-5')

    def test_uses_top_level_when_section_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / 'config.toml'
            config.write_text(
                '\n'.join(
                    [
                        'telegram_bot_token = "top"',
                        'allowed_chat_id = 123',
                        'unknown_key = "ignored"',
                    ]
                )
                + '\n',
                encoding='utf-8',
            )

            values = load_settings_from_toml(str(config))

            self.assertEqual(values, {'telegram_bot_token': 'top', 'allowed_chat_id': 123})
