from telecodex.__main__ import load_settings_from_toml


def test_load_settings_from_toml_reads_telecodex_section(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text(
        '\n'.join(
            [
                '[telecodex]',
                'telegram_bot_token = "token"',
                'allowed_chat_id = 90419297',
                'acp_log_file = "/tmp/acp.log"',
                'poll_timeout_seconds = 45',
                'codex_model = "gpt-5"',
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    values = load_settings_from_toml(str(config))

    assert values['telegram_bot_token'] == 'token'
    assert values['allowed_chat_id'] == 90419297
    assert values['acp_log_file'] == '/tmp/acp.log'
    assert values['poll_timeout_seconds'] == 45
    assert values['codex_model'] == 'gpt-5'


def test_load_settings_from_toml_uses_top_level_when_section_missing(tmp_path):
    config = tmp_path / 'config.toml'
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

    assert values == {'telegram_bot_token': 'top', 'allowed_chat_id': 123}
