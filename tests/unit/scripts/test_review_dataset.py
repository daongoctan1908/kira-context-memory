"""Review CLI refuses draft input and never leaks validation details."""

import json
import shutil

from evaluation.dataset import default_dataset_root
from scripts.benchmark.review_dataset import main
from tests.support.draft_dataset import copy_draft_dataset


def _rewind_policy(root):
    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["data_policy"]["external_provider_allowed"] = False
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_export_rejects_nonmaterialized_dataset_without_content(tmp_path, capsys):
    assert (
        main(
            [
                "export",
                "--root",
                str(copy_draft_dataset(tmp_path / "dataset")),
                "--packet",
                str(tmp_path / "packet.json"),
                "--decisions",
                str(tmp_path / "decisions.json"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "error_class=ValueError" in captured.err
    assert "TBD_AFTER_KIRA_FILL" not in captured.err + captured.out


def test_policy_cli_records_explicit_authorization_on_reviewed_dataset(tmp_path, capsys):
    root = tmp_path / "dataset"
    shutil.copytree(default_dataset_root(), root)
    _rewind_policy(root)
    audit = tmp_path / "audit.json"
    assert (
        main(
            [
                "policy",
                "--root",
                str(root),
                "--dataset-version",
                "pc-authorized.1",
                "--allow-pc-openai",
                "--actor",
                "owner",
                "--authorization-reference",
                "user-message",
                "--notes",
                "Explicit owner authorization for frozen corpus.",
                "--audit",
                str(audit),
                "--in-place",
            ]
        )
        == 0
    )
    assert json.loads(audit.read_text(encoding="utf-8"))["external_provider_allowed"] is True
    assert "external_provider_allowed=true" in capsys.readouterr().out
