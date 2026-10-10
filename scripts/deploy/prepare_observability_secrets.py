"""Generate private, stable Langfuse/metrics credentials without printing their values."""

import argparse
import base64
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

import yaml

from scripts.deploy.prepare_k8s_secrets import ROOT, secret


def document(admin_email: str) -> dict:
    if "@" not in admin_email or any(c.isspace() for c in admin_email):
        raise ValueError("Supply the Langfuse administrator email")
    values = {
        key: secrets.token_urlsafe(32)
        for key in (
            "LF_PG_ADMIN_PASSWORD",
            "LF_PG_PASSWORD",
            "LF_CLICKHOUSE_PASSWORD",
            "LF_REDIS_PASSWORD",
            "LF_S3_SECRET_KEY",
            "LANGFUSE_SALT",
            "LANGFUSE_NEXTAUTH_SECRET",
            "LANGFUSE_INIT_USER_PASSWORD",
            "GRAFANA_ADMIN_PASSWORD",
        )
    }
    values.update(
        {
            "LF_DATABASE_URL": (
                "postgresql://langfuse:"
                f"{quote(values['LF_PG_PASSWORD'], safe='')}@langfuse-postgres:5432/langfuse"
                "?connection_limit=15&pool_timeout=10"
            ),
            "LF_S3_ACCESS_KEY": "kira-" + secrets.token_hex(12),
            "LANGFUSE_ENCRYPTION_KEY": secrets.token_hex(32),
            "LANGFUSE_INIT_USER_EMAIL": admin_email,
            "LANGFUSE_PROJECT_PUBLIC_KEY": "pk-lf-" + secrets.token_hex(16),
            "LANGFUSE_PROJECT_SECRET_KEY": "sk-lf-" + secrets.token_hex(16),
        }
    )
    auth = f"{values['LANGFUSE_PROJECT_PUBLIC_KEY']}:{values['LANGFUSE_PROJECT_SECRET_KEY']}"
    values["LANGFUSE_OTEL_AUTH"] = "Basic " + base64.b64encode(auth.encode()).decode()
    return secret("kira-observability", values)


def prepare(admin_email: str, output: Path) -> None:
    destination = output.resolve()
    if destination.is_relative_to(ROOT) and not destination.is_relative_to(ROOT / "artifacts"):
        raise ValueError("Write private Secrets under ignored artifacts/ or outside the repository")
    if destination.exists():
        raise FileExistsError("Private Secret already exists; keep credentials with existing PVCs")
    data = document(admin_email)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(data, stream, sort_keys=False)
    destination.chmod(0o600)
    print(f"Private observability Secret prepared: {destination}")
    print("Preserve passwords, salt, encryption key and project keys with the data volumes.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.admin_email, args.output)
    except (OSError, ValueError) as error:
        print(f"Observability Secret preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
