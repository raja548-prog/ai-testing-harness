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


def test_missing_files_are_refused_not_a_traceback(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    assert main(["resume", "r1", "--profile", str(tmp_path / "missing.yaml"), "--db", db]) == 1
    assert "refused: [Errno 2]" in capsys.readouterr().out
    assert main(["run", "--profile", str(tmp_path / "missing.yaml"), "--db", db,
                 "--commit", "mr001", "--merge-base", "base000", "--env", "uat1"]) == 1
    assert main(["run", "--profile", str(LOCAL_PROFILE), "--db", db, "--quarantine", str(tmp_path / "q.txt"),
                 "--commit", "mr001", "--merge-base", "base000", "--env", "uat1"]) == 1
    assert main(["run", "--profile", str(LOCAL_PROFILE), "--db", db, "--run-id", "../x",
                 "--commit", "mr001", "--merge-base", "base000", "--env", "uat1"]) == 1
