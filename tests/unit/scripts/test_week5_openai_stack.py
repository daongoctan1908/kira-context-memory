from pathlib import Path

import pytest

from scripts.week5_openai_stack import (
    compose_command,
    compose_environment,
    load_env_file,
)


def test_file_values_override_inherited_openai_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.week5.local"
    env_file.write_text(
        "\n".join(
            (
                'OPENAI_API_KEY="file-key"',
                "WEEK5_OPENAI_CHAT_MODEL=gpt-test",
                "WEEK5_OPENAI_EMBEDDING_MODEL=embedding-test",
            )
        ),
        encoding="utf-8",
    )

    values = load_env_file(env_file)
    environment = compose_environment(values, {"OPENAI_API_KEY": "inherited-key"})

    assert environment["OPENAI_API_KEY"] == "file-key"


def test_load_env_file_rejects_missing_required_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.week5.local"
    env_file.write_text("OPENAI_API_KEY=key\n", encoding="utf-8")

    with pytest.raises(ValueError, match="WEEK5_OPENAI_CHAT_MODEL"):
        load_env_file(env_file)


def test_compose_command_never_contains_secret() -> None:
    command = compose_command("up", Path("local.env"))

    assert command[-4:] == ["up", "-d", "--build", "--wait"]
    assert "OPENAI_API_KEY" not in " ".join(command)
