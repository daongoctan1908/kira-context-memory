"""Review CLI refuses draft input and never leaks validation details."""

from scripts.benchmark.review_dataset import main
from tests.support.draft_dataset import copy_draft_dataset


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
