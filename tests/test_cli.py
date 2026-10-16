from ath_cli.main import main
from tests.conftest import LOCAL_PROFILE, PROD_PROFILE


def test_cli_run_and_report(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    rc = main(["run", "--profile", str(LOCAL_PROFILE), "--db", db, "--report-dir", str(tmp_path),
               "--commit", "mr001", "--merge-base", "base000", "--env", "uat1", "--run-id", "c1", "--quiet"])
    assert rc == 0
    assert main(["report", "c1", "--db", db]) == 0
    assert "ATH report: settlenxt" in capsys.readouterr().out


def test_doctor_flags_missing_adapter(capsys):
    assert main(["doctor", str(PROD_PROFILE)]) == 2
    assert "MISSING" in capsys.readouterr().out
