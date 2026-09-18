"""Review CLI refuses draft input and never leaks validation details."""

from evaluation.dataset import default_dataset_root
from scripts.review_dataset import main


def test_export_rejects_nonmaterialized_dataset_without_content(tmp_path, capsys):
    assert (
        main(
            [
                "export",
                "--root",
                str(default_dataset_root()),
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
