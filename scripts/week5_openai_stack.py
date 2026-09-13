"""Run the Week 5 OpenAI Compose stack with file-local configuration precedence."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE_FILE = REPOSITORY_ROOT / "compose.week4.yaml"
OPENAI_COMPOSE_FILE = REPOSITORY_ROOT / "compose.week5.openai.yaml"
DEFAULT_ENV_FILE = REPOSITORY_ROOT / ".env.week5.local"
REQUIRED_VALUES = (
    "OPENAI_API_KEY",
    "WEEK5_OPENAI_CHAT_MODEL",
    "WEEK5_OPENAI_EMBEDDING_MODEL",
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_env_file(path: Path) -> dict[str, str]:
    """Read the small KEY=VALUE file without logging any values."""
    if not path.is_file():
        raise ValueError(f"environment file not found: {path}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment entry at line {line_number}")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(f"invalid environment name at line {line_number}")
        values[name] = _unquote(raw_value.strip(), line_number)

    missing = [name for name in REQUIRED_VALUES if not values.get(name, "").strip()]
    if missing:
        raise ValueError(f"missing required environment values: {', '.join(missing)}")
    return values


def compose_environment(
    file_values: Mapping[str, str],
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Make file values override inherited variables for Compose interpolation."""
    environment = dict(os.environ if inherited is None else inherited)
    environment.update(file_values)
    return environment


def compose_command(action: str, env_file: Path, *, volumes: bool = False) -> list[str]:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(BASE_COMPOSE_FILE),
        "-f",
        str(OPENAI_COMPOSE_FILE),
    ]
    if action == "up":
        return [*command, "up", "-d", "--build", "--wait"]
    if action == "down":
        return [*command, "down", *(["-v"] if volumes else [])]
    if action == "ps":
        return [*command, "ps", "-a"]
    if action == "config":
        return [*command, "config", "--services"]
    raise ValueError(f"unsupported action: {action}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "down", "ps", "config"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--volumes",
        action="store_true",
        help="with down, permanently remove the synthetic PostgreSQL volume",
    )
    args = parser.parse_args(argv)
    if args.volumes and args.action != "down":
        parser.error("--volumes is only valid with down")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    env_file = args.env_file.resolve()
    try:
        file_values = load_env_file(env_file)
        environment = compose_environment(file_values)
        command = compose_command(args.action, env_file, volumes=args.volumes)
        return subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
        ).returncode
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("docker executable not found", file=sys.stderr)
        return 3


def _unquote(value: str, line_number: int) -> str:
    if not value or value[0] not in {'"', "'"}:
        return value
    if len(value) < 2 or value[-1] != value[0]:
        raise ValueError(f"unterminated quoted value at line {line_number}")
    return value[1:-1]


if __name__ == "__main__":
    raise SystemExit(main())
