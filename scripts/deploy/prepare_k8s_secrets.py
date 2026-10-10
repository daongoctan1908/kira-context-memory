"""Prepare private Kubernetes Secrets from existing KiRa credentials and new DB passwords."""

import argparse
import base64
import getpass
import json
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
NAMESPACE = "kira-context-memory"


def secret(name: str, values: dict[str, str], kind: str = "Opaque") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "type": kind,
        "stringData": values,
    }


def documents(kira: dict, registry_user: str | None, registry_password: str | None) -> list[dict]:
    for key in ("KIRA_USERNAME", "KIRA_BASIC_AUTH"):
        if not kira.get(key) or kira[key] == "replace_me":
            raise ValueError(f"Existing KiRa configuration is missing {key}")
    if kira.get("KIRA_BASE_URL", "").rstrip("/") != "http://10.255.62.64:8122":
        raise ValueError(
            "KiRa URL differs from the reviewed ConfigMap; reconcile before deployment"
        )
    if kira.get("KIRA_DOMAIN", "VBI") != "VBI":
        raise ValueError("KiRa domain differs from the reviewed ConfigMap")
    admin, app, memory = (secrets.token_urlsafe(32) for _ in range(3))

    def dsn(user: str, password: str, asynchronous: bool = False) -> str:
        scheme = "postgresql+asyncpg" if asynchronous else "postgresql"
        return f"{scheme}://{user}:{quote(password, safe='')}@postgres:5432/kira_context"

    auth = {}
    if registry_user is not None:
        if not registry_user or not registry_password:
            raise ValueError("Registry user and password must both be present")
        encoded = base64.b64encode(f"{registry_user}:{registry_password}".encode()).decode()
        auth = {"registry.vlp.vn": {"auth": encoded}}
    return [
        secret(
            "postgres-bootstrap",
            {
                "POSTGRES_PASSWORD": admin,
                "KIRA_APP_DB_PASSWORD": app,
                "KIRA_MEMORY_DB_PASSWORD": memory,
            },
        ),
        secret(
            "kira-database",
            {
                "DATABASE_URL": dsn("kira_app", app, True),
                "MEMORY_DATABASE_URL": dsn("kira_memory", memory),
            },
        ),
        secret(
            "kira-database-admin",
            {
                "MIGRATION_DATABASE_URL": dsn("postgres", admin, True),
                "MEMORY_ADMIN_DATABASE_URL": dsn("postgres", admin),
            },
        ),
        secret("kira-service", {key: kira[key] for key in ("KIRA_USERNAME", "KIRA_BASIC_AUTH")}),
        secret(
            "registry-vlp",
            {".dockerconfigjson": json.dumps({"auths": auth})},
            "kubernetes.io/dockerconfigjson",
        ),
    ]


def prepare(kira_env: Path, output: Path, registry_user: str | None = None) -> None:
    destination = output.resolve()
    if destination.is_relative_to(ROOT) and not destination.is_relative_to(ROOT / "artifacts"):
        raise ValueError("Write private Secrets under ignored artifacts/ or outside the repository")
    if destination.exists():
        raise FileExistsError(
            "Private Secret file already exists; do not regenerate DB credentials"
        )
    kira = dict(dotenv_values(kira_env, interpolate=False))
    password = (
        getpass.getpass("Registry pull password/token: ") if registry_user is not None else None
    )
    docs = documents(kira, registry_user, password)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump_all(docs, stream, sort_keys=False)
    destination.chmod(0o600)
    print(f"Private Secret file prepared: {destination}")
    print("Keep the same DB passwords with this PVC; this command does not rotate existing roles.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kira-env", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--registry-user", help="Optional pull identity; password/token is prompted"
    )
    args = parser.parse_args()
    try:
        prepare(args.kira_env, args.output, args.registry_user)
    except (OSError, ValueError) as error:
        print(f"Secret preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
