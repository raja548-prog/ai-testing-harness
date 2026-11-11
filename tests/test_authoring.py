"""T3 test authoring: engine, guards, masking, formats, standalone `author` command and T3 in a run."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ath_cli.main import main
from ath_core.adapters import AdapterError, registry
from ath_core.authoring.engine import (
    PLACEHOLDER, AuthorInputs, AuthoringEngine, Workspace, load_proposal, module_globs, save_proposal,
)
from ath_core.authoring.formats import TextFile, glob_match
from ath_core.authoring.signatures import generic_signatures, java_signatures, signatures
from ath_core.authoring.testdata import catalogue_problems
from ath_core.context import select_tsd_sections, tsd_clause_ids
from ath_core.masking import Masker
from ath_core.policy import load_policy
from ath_core.schemas import RunContext, RunMode, RunStatus, Stage, StageStatus, load_profile
from ath_core.schemas.atrf import ATRFCase, ATRFFailure, ATRFRun, CaseStatus, Triage, TriageCategory
from ath_core.schemas.authoring import Proposal
from ath_server.orchestrator import Orchestrator, StateStore
from tests.conftest import LOCAL_PROFILE, PROD_PROFILE, ROOT

FIX = ROOT / "tests/fixtures/authoring"
TSD_FILE = FIX / "tsd.md"
TSD = TSD_FILE.read_text()
FAKE = ROOT / "tests/fakes/fake_agent_cli.py"
OWNERS = ["settlement-dept"]
JAVA_TEST = "src/test/java/com/settle/clearing/FeeCalculatorTest.java"
LABEL_TEST = "src/test/java/com/settle/portal/LabelTest.java"
NEW_TEST = "src/test/java/com/settle/clearing/RefundFeeTest.java"
FEES = "testdata/clearing/fees.csv"

ASSERT_LINE = '        assertEquals(new BigDecimal("12.50"), fee);\n'
UPDATE = {"action": "update", "path": JAVA_TEST, "clauses": ["AC-7"], "reason": "half-up rounding (AC-7)",
          "edits": [{"find": ASSERT_LINE, "replace": ASSERT_LINE + '        assertEquals(new BigDecimal("15.63"), '
                     'calc.interchangeFee(CARD, new BigDecimal("1250.00")));\n'}]}
CREATE = {"action": "create", "path": NEW_TEST, "clauses": ["AC-8", "R-4"], "reason": "refund reverses the fee",
          "content": "package com.settle.clearing;\n\nclass RefundFeeTest {\n"
                     "    private static final String CARD = \"{{testdata:visa-credit-01.pan}}\";\n\n"
                     "    @Test\n    void refundReversesFee() {\n        assertEquals(1, 1);\n    }\n}\n"}


def answer(*changes, questions=(), summary="done") -> dict:
    return {"changes": list(changes), "spec_questions": list(questions), "summary": summary}


class StubAgent:
    """In-process author: returns a fixed answer and records the blocks it was sent."""
    model = "local/onprem-coder"

    def __init__(self, out: dict):
        self.out, self.calls, self.blocks = out, 0, {}
        self.last_call: dict = {}

    def run(self, blocks):
        self.calls += 1
        self.blocks = blocks
        self.last_call = {"model": self.model, "prompt_sha256": "p" * 64}
        return self.out


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path) -> Path:
    """A copy of the fixture test repo, committed to its own Git repository."""
    dest = tmp_path / "tests-checkout"
    shutil.copytree(FIX / "repo", dest)
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, "commit", "-qm", "init")
    return dest


@pytest.fixture
def profile():
    return load_profile(LOCAL_PROFILE)


def profile_with(tmp_path, **authoring) -> Path:
    """The local profile with testAuthoring fields replaced, written next to its product skill."""
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    data["testAuthoring"].update(authoring)
    for k in [k for k, v in authoring.items() if v is None]:
        del data["testAuthoring"][k]
    path = LOCAL_PROFILE.parent / f".tmp-profile-{tmp_path.name}.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture(autouse=True)
def _cleanup_profiles():
    yield
    for p in LOCAL_PROFILE.parent.glob(".tmp-profile-*.yaml"):
        p.unlink()


def propose(profile, checkout, out, **inputs):
    agent = StubAgent(out)
    drafted = AuthoringEngine(profile, agent).propose(AuthorInputs(tsd=inputs.pop("tsd", TSD), tests_dir=checkout,
                                                                   **inputs), "standalone")
    return drafted, agent


def dropped(drafted) -> dict[str, str]:
    return {d.path: d.reason for d in drafted.proposal.dropped}


def apply(profile, checkout, proposal):
    return registry.create("author", "local-fs", profile=profile).apply(proposal, checkout, "lead-ravi")


# ------------------------------------------------------------------------------------- mask_spans
def test_mask_spans_map_every_unmasked_byte_back_to_the_original():
    rng = random.Random(7)
    pieces = ["4111 1111 1111 1111", "5500005555555559", "cvv=123", "\"cvv\": \"456\"", "9876543210",
              "+91 9876543210", "2345 6789 0124", ";4111111111111111=2512101?", "pin_block=0123456789ABCDEF",
              "rrn=234567890124", "x", " ", "\n", "abc", "12", "1234567890123", "DE002=164111111111111111"]
    for _ in range(400):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 12)))
        masked, spans = Masker(correlation_keys=["rrn"]).mask_spans(text)
        assert masked == Masker(correlation_keys=["rrn"]).mask(text)
        po = pm = 0
        for s in spans:
            assert s.orig_start >= po and s.masked_start >= pm
            assert text[po:s.orig_start] == masked[pm:s.masked_start]
            po, pm = s.orig_end, s.masked_end
        assert text[po:] == masked[pm:]


# --------------------------------------------------------------------------------- context helpers
def test_tsd_clause_ids_and_sections():
    assert tsd_clause_ids(TSD) == {"R-1", "R-3", "AC-7", "R-4", "AC-8", "IMP-2"}
    assert select_tsd_sections(TSD, ["AC-7"]).startswith("## 3.2 Fee rounding")
    assert "Portal" in select_tsd_sections(TSD, ["2 portal labels"])
    with pytest.raises(ValueError, match="matches no TSD section"):
        select_tsd_sections(TSD, ["AC-99"])


def test_glob_match():
    assert glob_match("src/test/A.java", "src/test/**/*.java")
    assert glob_match("src/test/x/y/A.java", "src/test/**/*.java")
    assert not glob_match("src/main/A.java", "src/test/**/*.java")
    assert not glob_match("testdata/a/b.csv", "testdata/*.csv")


def test_module_globs_from_product_skill():
    text = (ROOT / "examples/settlenxt/skills/settlenxt-product-skill.md").read_text()
    assert module_globs(text) == ["src/main/java/com/settle/clearing/**", "src/main/java/com/settle/portal/**"]


# ------------------------------------------------------------------------------- standalone engine
def test_create_and_update_are_applied(profile, checkout):
    drafted, agent = propose(profile, checkout, answer(UPDATE, CREATE))
    p = drafted.proposal
    assert [c.path for c in p.changes] == [JAVA_TEST, NEW_TEST] and not p.dropped and agent.calls == 1
    result = apply(profile, checkout, p)
    assert result.applied == [JAVA_TEST, NEW_TEST]
    updated = (checkout / JAVA_TEST).read_text()
    assert "15.63" in updated and 'CARD = "4111 1111 1111 1111"' in updated  # raw PAN untouched on disk
    assert '"4111111111111111"' in (checkout / NEW_TEST).read_text()       # catalogue value filled in
    # The stored artifacts keep references and masked text, never values.
    for text in (p.canonical_json(), drafted.patch, drafted.checklist):
        assert "4111111111111111" not in text and "4111 1111 1111 1111" not in text
    assert "{{testdata:visa-credit-01.pan}}" in drafted.patch


def test_applying_twice_is_harmless(profile, checkout):
    drafted, _ = propose(profile, checkout, answer(UPDATE, CREATE))
    apply(profile, checkout, drafted.proposal)
    again = apply(profile, checkout, drafted.proposal)
    assert again.applied == [] and again.already_applied == [JAVA_TEST, NEW_TEST]


def test_proposal_round_trips_with_its_hash(profile, checkout, tmp_path):
    drafted, _ = propose(profile, checkout, answer(UPDATE))
    h = save_proposal(tmp_path / "p", drafted)
    assert load_proposal(tmp_path / "p").hash == h == drafted.proposal.hash
    body = json.loads((tmp_path / "p/proposal.json").read_text())
    body["summary"] = "tampered"
    (tmp_path / "p/proposal.json").write_text(json.dumps(body))
    with pytest.raises(ValueError, match="recorded hash"):
        load_proposal(tmp_path / "p")


def _symlink_out(checkout, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (checkout / "src/test/java/evil").symlink_to(outside, target_is_directory=True)
    return {**CREATE, "path": "src/test/java/evil/X.java"}


@pytest.mark.parametrize("change, why", [
    ({**CREATE, "path": "../escape/X.java"}, "without '..'"),
    ({**CREATE, "path": "/etc/X.java"}, "without '..'"),
    ({**CREATE, "path": "src/main/java/Foo.java"}, "matches no testAuthoring.sources"),
    ({**CREATE, "path": ".gitlab-ci.yml"}, "hidden"),
    ({**CREATE, "clauses": ["AC-99"]}, "not ⓘ ids"),
    ({**CREATE, "clauses": ["3.2 Fee rounding"]}, "cite the ⓘ ids"),
    ({**CREATE, "clauses": []}, "cites no TSD clause"),
    ({**UPDATE, "edits": [{"find": ASSERT_LINE, "replace": "String c = \"[PAN****1111]\";\n"}]}, "mask token"),
    ({**UPDATE, "edits": [{"find": "assertEquals", "replace": "assertSame"}]}, "occurs 2 times"),
    ({**UPDATE, "edits": [{"find": "not in the file", "replace": "x"}]}, "not in the file"),
    ({**UPDATE, "edits": [{"find": "assertEquals(new", "replace": "x"}, {"find": "new BigDecimal(\"12.50\")",
                                                                      "replace": "y"}]}, "overlap"),
    ({**UPDATE, "edits": [{"find": "1111", "replace": "2222"}]}, "edit touches masked data"),
    ({**CREATE, "path": JAVA_TEST}, "does not exist"),
    ({**UPDATE, "path": NEW_TEST}, "file that exists"),
    ({**CREATE, "content": "class A { String pan = \"4012888888881881\"; }"}, "card number ([PAN****1881])"),
    ({**CREATE, "content": "class A { String m = \"9123456789\"; }"}, "mobile number ([MOBILE])"),
    ({**CREATE, "content": "class A { String p = \"{{testdata:nope-01.pan}}\"; }"}, "unknown test-data reference"),
    ({**UPDATE, "edits": [{"find": ASSERT_LINE, "replace": ASSERT_LINE}]}, "change nothing"),
    ({**UPDATE, "content": "x"}, "edits, not content"),
])
def test_guards_drop_the_change_with_a_reason(profile, checkout, change, why):
    drafted, _ = propose(profile, checkout, answer(change))
    assert drafted.proposal.changes == []
    assert why in dropped(drafted)[change["path"]]


def test_symlink_escape_is_dropped(profile, checkout, tmp_path):
    change = _symlink_out(checkout, tmp_path)
    drafted, _ = propose(profile, checkout, answer(change))
    assert "leaves the test checkout" in dropped(drafted)[change["path"]]


def test_model_free_text_is_masked_in_the_proposal(profile, checkout):
    out = answer({**UPDATE, "reason": "card 4111111111111111"}, questions=[{"question": "mobile 9876543210?"}],
                 summary="used 5500005555555559")
    drafted, _ = propose(profile, checkout, out)
    stored = drafted.proposal.canonical_json() + drafted.checklist
    for raw in ("4111111111111111", "9876543210", "5500005555555559"):
        assert raw not in stored


def test_malformed_change_is_dropped_and_the_rest_kept(profile, checkout):
    drafted, _ = propose(profile, checkout, answer(UPDATE, {"action": "delete", "path": "x.java"}))
    assert [c.path for c in drafted.proposal.changes] == [JAVA_TEST]
    assert "malformed change" in dropped(drafted)["x.java"]


def test_update_needs_the_file_sent_whole(tmp_path, checkout):
    prof = load_profile(profile_with(tmp_path, limits={"testFiles": 1}))
    label = {"action": "update", "path": LABEL_TEST, "clauses": ["R-1"],
             "edits": [{"find": "Settlement\"", "replace": "Settlements\""}]}
    drafted, agent = propose(prof, checkout, answer({**label, "clauses": ["AC-7"]}), sections=["AC-7"])
    assert LABEL_TEST not in agent.blocks["tests"].split("more test file(s)")[0]
    assert "not sent whole" in dropped(drafted)[LABEL_TEST]


def test_section_titles_count_only_without_ids(profile, checkout):
    tsd = "# TSD\n\n## Fee rounding\nFees round half-up.\n"
    drafted, _ = propose(profile, checkout, answer({**CREATE, "clauses": ["Fee rounding"]}), tsd=tsd)
    assert drafted.proposal.changes[0].traced_by == "title"
    assert "traced by section title only" in drafted.checklist


def test_base_file_changed_after_the_proposal_writes_nothing(profile, checkout):
    drafted, _ = propose(profile, checkout, answer(CREATE, UPDATE))
    (checkout / JAVA_TEST).write_text((checkout / JAVA_TEST).read_text() + "// edited by hand\n")
    with pytest.raises(AdapterError, match="changed after the proposal"):
        apply(profile, checkout, drafted.proposal)
    assert not (checkout / NEW_TEST).exists()  # all files are checked first, then written


def test_max_changes_cap(tmp_path, checkout):
    prof = load_profile(profile_with(tmp_path, limits={"maxChanges": 1}))
    drafted, _ = propose(prof, checkout, answer(UPDATE, CREATE))
    assert len(drafted.proposal.changes) == 1 and "over the limit" in dropped(drafted)[NEW_TEST]


# ------------------------------------------------------------------------------------ masked data
def test_test_data_is_masked_in_the_prompt(profile, checkout):
    _, agent = propose(profile, checkout, answer(UPDATE))
    tests = agent.blocks["tests"]
    assert "[PAN****1111]" in tests and "4111" not in tests.replace("[PAN****1111]", "")
    assert "5500005555555559" not in tests and "editable: true" in tests


def test_crlf_and_bom_survive_an_update(profile, checkout):
    raw = (checkout / JAVA_TEST).read_bytes().replace(b"\n", b"\r\n")
    (checkout / JAVA_TEST).write_bytes(b"\xef\xbb\xbf" + raw)
    drafted, _ = propose(profile, checkout, answer(UPDATE))
    apply(profile, checkout, drafted.proposal)
    data = (checkout / JAVA_TEST).read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and b"15.63" in data
    assert data.count(b"\n") == data.count(b"\r\n")


def test_non_utf8_files_are_not_sent(profile, checkout):
    (checkout / "src/test/java/com/settle/Latin1Test.java").write_bytes("// caf\xe9\n".encode("latin-1"))
    _, agent = propose(profile, checkout, answer(UPDATE))
    assert "not UTF-8" in agent.blocks["tests"] and "Latin1Test" in agent.blocks["tests"]
    assert TextFile.decode(b"\xff\xfe") is None


# -------------------------------------------------------------------------------------------- csv
def csv_change(*edits, path=FEES, clauses=("AC-7",)):
    return {"action": "update", "path": path, "clauses": list(clauses), "edits": list(edits)}


def test_csv_add_row_fills_the_catalogue_value(profile, checkout):
    edit = {"op": "add_row", "values": {"test_id": "FEE-003", "pan": "{{testdata:mc-debit-01.pan}}",
                                        "amount": 1249.995, "expected_fee": "15.62"}}
    drafted, _ = propose(profile, checkout, answer(csv_change(edit)))
    apply(profile, checkout, drafted.proposal)
    lines = (checkout / FEES).read_text().splitlines()
    assert lines[-1] == "FEE-003,5500005555555559,1249.995,15.62"
    assert "{{testdata:mc-debit-01.pan}}" in drafted.patch and "5500005555555559" not in drafted.patch


def test_csv_update_row_changes_only_named_columns(profile, checkout):
    before = (checkout / FEES).read_text().splitlines()
    drafted, _ = propose(profile, checkout, answer(csv_change({"op": "update_row", "key": "FEE-002",
                                                               "set": {"expected_fee": "3.12"}})))
    apply(profile, checkout, drafted.proposal)
    after = (checkout / FEES).read_text().splitlines()
    assert after[:2] == before[:2]  # header and FEE-001 byte-identical
    assert after[2] == "FEE-002,5500005555555559,250.00,3.12"  # masked PAN kept its original value


@pytest.mark.parametrize("edit, why", [
    ({"op": "update_row", "key": "FEE-001", "set": {"nope": "1"}}, "unknown column"),
    ({"op": "update_row", "key": "FEE-404", "set": {"amount": "1"}}, "no row has"),
    ({"op": "add_row", "values": {"bogus": "1"}}, "unknown column"),
    ({"find": "FEE-001", "replace": "FEE-009"}, "add_row / update_row"),
])
def test_csv_bad_row_edits_are_dropped(profile, checkout, edit, why):
    drafted, _ = propose(profile, checkout, answer(csv_change(edit)))
    assert why in dropped(drafted)[FEES]


def test_csv_duplicate_key_is_dropped(profile, checkout):
    (checkout / FEES).write_text((checkout / FEES).read_text() + "FEE-002,4111111111111111,9.00,0.11\n")
    git(checkout, "commit", "-qam", "dup")
    drafted, _ = propose(profile, checkout, answer(csv_change({"op": "update_row", "key": "FEE-002",
                                                               "set": {"amount": "1"}})))
    assert "2 rows have" in dropped(drafted)[FEES]


def test_csv_key_column_holding_masked_values_is_refused(tmp_path, checkout):
    prof = load_profile(profile_with(tmp_path, sources=[
        {"kind": "code", "paths": ["src/test/**/*.java"]},
        {"kind": "data", "format": "csv", "paths": ["testdata/**/*.csv"], "keyColumn": "pan"}]))
    for key, why in (("[PAN****1111]", "mask token"), ("4111111111111111", "card number")):
        drafted, _ = propose(prof, checkout, answer(csv_change({"op": "update_row", "key": key,
                                                                "set": {"amount": "1"}})))
        assert why in dropped(drafted)[FEES]
        assert "4111111111111111" not in drafted.proposal.canonical_json()


@pytest.mark.parametrize("text", [
    'test_id;pan;amount;expected_fee\nFEE-001;4111111111111111;1000,00;12,50\nFEE-002;x;2;3\n',
    '"test_id","pan","amount","expected_fee"\n"FEE-001","4111111111111111","1000.00","12.50"\n"FEE-002","x","2","3"\n',
    'test_id,pan,amount,expected_fee\nFEE-001,"4111111111111111","1,000.00",12.50\nFEE-002,x,2,3\n',
])
def test_csv_untouched_lines_are_byte_identical(profile, checkout, text):
    (checkout / FEES).write_text(text)
    git(checkout, "commit", "-qam", "dialect")
    drafted, _ = propose(profile, checkout, answer(csv_change(
        {"op": "update_row", "key": "FEE-002", "set": {"amount": "5"}},
        {"op": "add_row", "values": {"test_id": "FEE-003", "amount": "7"}})))
    assert drafted.proposal.changes, dropped(drafted)
    apply(profile, checkout, drafted.proposal)
    old, new = text.splitlines(), (checkout / FEES).read_text().splitlines()
    assert new[:2] == old[:2] and len(new) == 4
    delim = ";" if ";" in old[0] else ","
    quoted = old[0].startswith('"')
    assert new[2].split(delim)[2] == ('"5"' if quoted else "5")
    assert new[3].startswith('"FEE-003"' if quoted else "FEE-003")


def test_new_csv_must_copy_a_sibling_header(profile, checkout):
    good = {"action": "create", "path": "testdata/clearing/refunds.csv", "clauses": ["AC-8"],
            "content": "test_id,pan,amount,expected_fee\nREF-001,{{testdata:visa-credit-01.pan}},10.00,-0.13\n"}
    bad = {**good, "path": "testdata/clearing/other.csv", "content": "id,amount\n1,2\n"}
    lonely = {**good, "path": "testdata/new-folder/x.csv"}
    drafted, _ = propose(profile, checkout, answer(good, bad, lonely))
    assert [c.path for c in drafted.proposal.changes] == [good["path"]]
    assert "header of the files next to it" in dropped(drafted)[bad["path"]]
    assert "needs a sibling" in dropped(drafted)[lonely["path"]]


# ------------------------------------------------------------------------------------- json, yaml
def test_json_edit_that_breaks_parsing_is_dropped(profile, checkout):
    change = {"action": "update", "path": "scenarios/refund.json", "clauses": ["AC-8"],
              "edits": [{"find": '"amount": "1000.00",', "replace": '"amount": 1000.00,,'}]}
    drafted, _ = propose(profile, checkout, answer(change))
    assert "no longer parses as JSON" in dropped(drafted)["scenarios/refund.json"]


def test_yaml_comments_survive_an_update(profile, checkout):
    change = {"action": "update", "path": "scenarios/refund.yaml", "clauses": ["AC-8"],
              "edits": [{"find": 'expectedFee: "-12.50"', "replace": 'expectedFee: "-12.50"\ncard: "{{testdata:visa-credit-01.pan}}"'}]}
    drafted, _ = propose(profile, checkout, answer(change))
    apply(profile, checkout, drafted.proposal)
    text = (checkout / "scenarios/refund.yaml").read_text()
    assert "# Refund scenario" in text and "# original purchase" in text
    assert yaml.safe_load(text)["card"] == "4111111111111111"


def test_yaml_parse_error_message_quotes_no_content(profile, checkout):
    change = {"action": "update", "path": "scenarios/refund.yaml", "clauses": ["AC-8"],
              "edits": [{"find": "scenario: refund-full", "replace": "scenario: [refund-full"}]}
    drafted, _ = propose(profile, checkout, answer(change))
    reason = dropped(drafted)["scenarios/refund.yaml"]
    assert "no longer parses as YAML" in reason and "refund-full" not in reason


# ----------------------------------------------------------------------------------- review flags
def test_review_flags_reach_the_checklist(profile, checkout):
    change = {"action": "update", "path": JAVA_TEST, "clauses": ["AC-7"], "edits": [
        {"find": "import java.math.BigDecimal;\n", "replace": "import java.math.BigDecimal;\nimport java.net.URL;\n"},
        {"find": "    @Test\n", "replace": "    @Disabled\n    @Test\n"},
        {"find": ASSERT_LINE, "replace": "        Runtime.getRuntime().exec(\"id\");\n"}]}
    drafted, _ = propose(profile, checkout, answer(change))
    flags = " ".join(drafted.proposal.changes[0].flags)
    for expected in ("weakens checks", "disables or skips", "new imports: import java.net.URL",
                     "Runtime.getRuntime"):
        assert expected in flags and expected in drafted.checklist


# ----------------------------------------------------------------------------------------- inputs
def test_prompt_blocks_and_placeholders(profile, checkout):
    _, agent = propose(profile, checkout, answer(UPDATE))
    b = agent.blocks
    assert list(b) == ["tsd", "tests", "tree", "conventions", "testdata", "product", "code", "templates", "diff",
                       "impact", "outdated"]
    assert "ⓘ R-3 · AC-7" in b["tsd"] and "SettleNxt test conventions" in b["conventions"]
    assert "SettleNxt product skill" in b["product"] and "refund-template.yaml" in b["templates"]
    assert b["code"] == b["diff"] == b["impact"] == b["outdated"] == PLACEHOLDER
    assert "visa-credit-01 (card)" in b["testdata"] and "fields: cvv, expiry, pan" in b["testdata"]
    for value in ("4111111111111111", "5500005555555559", "9876543210", "234567890124", "123"):
        assert value not in b["testdata"]  # catalogue values never reach the model
    assert JAVA_TEST in b["tree"] and "testdata/catalogue.yaml" in b["tree"]


def test_same_inputs_give_the_same_prompt(profile, checkout):
    from ath_core.agents.cli import build_prompt, load_skill
    skill = load_skill(ROOT / "skills/t03-author/SKILL.md")
    _, a1 = propose(profile, checkout, answer(UPDATE), code_dir=FIX / "code", diff="")
    _, a2 = propose(profile, checkout, answer(UPDATE), code_dir=FIX / "code", diff="")
    assert build_prompt(skill, a1.blocks) == build_prompt(skill, a2.blocks)


def test_oversize_blocks_say_what_was_left_out_and_never_cut_a_test_file(tmp_path, checkout):
    prof = load_profile(profile_with(tmp_path, limits={"testsChars": 1000, "treePaths": 10}))
    for i in range(5):
        (checkout / f"docs-{i}.md").write_text("notes\n")
    _, agent = propose(prof, checkout, answer(UPDATE))
    tests = agent.blocks["tests"]
    assert "more test file(s) not shown" in tests and "(0 more" not in tests
    assert tests.count("===== file:") == tests.count("===== end of file:")
    assert "more path(s) not shown" in agent.blocks["tree"]


def test_code_block_has_signatures_ranked_by_the_tsd(profile, checkout):
    _, agent = propose(profile, checkout, answer(UPDATE), code_dir=FIX / "code")
    code = agent.blocks["code"]
    assert "public BigDecimal interchangeFee(String pan, BigDecimal amount);" in code
    assert "RoundingMode.HALF_UP" not in code  # no bodies
    assert code.index("FeeCalculator.java") < code.index("Labels.java")


def test_diff_and_outdated_blocks(profile, checkout):
    diff = ("diff --git a/src/main/java/com/settle/clearing/FeeCalculator.java b/src/main/java/com/settle/clearing/FeeCalculator.java\n"
            "--- a/x\n+++ b/src/main/java/com/settle/clearing/FeeCalculator.java\n@@ -1 +1 @@\n"
            "-        return x.setScale(2, RoundingMode.DOWN);\n+        return x.setScale(2, RoundingMode.HALF_UP);\n")
    outdated = [{"id": "SNX-010", "suite": "smoke", "name": "fee rounds down",
                 "message": "expected 12.49 but was 12.50 for card 4111111111111111"}]
    _, agent = propose(profile, checkout, answer(UPDATE), diff=diff, outdated=outdated)
    assert "RoundingMode.HALF_UP" in agent.blocks["diff"]
    assert "SNX-010" in agent.blocks["outdated"] and "4111111111111111" not in agent.blocks["outdated"]


def test_sections_limit_the_tsd(profile, checkout):
    _, agent = propose(profile, checkout, answer(UPDATE), sections=["AC-7"])
    assert "Fee rounding" in agent.blocks["tsd"] and "Portal" not in agent.blocks["tsd"]


# ------------------------------------------------------------------------------------- signatures
def test_java_signatures():
    src = (FIX / "code/src/main/java/com/settle/clearing/FeeCalculator.java").read_text()
    sig = java_signatures(src)
    assert "public BigDecimal interchangeFee(String pan, BigDecimal amount);" in sig  # joined over two lines
    assert "@Deprecated protected <T extends Comparable<T>> List<T> sorted(List<T> items, " \
           "Map<String, ? extends T> extra);" in sig
    assert "public static class Summary {" in sig and "audit" not in sig and "multiply" not in sig
    rec = java_signatures((FIX / "code/src/main/java/com/settle/clearing/model/FeeRequest.java").read_text())
    assert "public record FeeRequest(String pan, BigDecimal amount, Channel channel) {" in rec
    assert "POS, ECOM, ATM;" in rec
    model = java_signatures((FIX / "code/src/main/java/com/settle/clearing/model/FeeResponse.java").read_text())
    assert '@JsonProperty("fee_amount") private BigDecimal feeAmount;' in model and "serialVersionUID" not in model


def test_unparseable_java_falls_back_to_declaration_lines():
    sig, note = signatures("Broken.java", "public class Broken {\n  public void a() {\n")
    assert note and "declaration lines only" in note and "public class Broken" in sig
    assert "def run(self, x)" in generic_signatures("class A:\n    def run(self, x):\n        return 1\n")


# -------------------------------------------------------------------------------------- catalogue
def test_catalogue_checks():
    bad = {"entries": [{"name": "c", "kind": "card", "values": {"pan": "4111111111111112"}},
                       {"name": "c", "kind": "aadhaar", "values": {"number": "234567890123"}},
                       {"name": "m", "kind": "pager", "values": {"number": "1"}}]}
    problems = " ".join(catalogue_problems(bad))
    assert "Luhn" in problems and "duplicate" in problems and "Verhoeff" in problems and "pager" in problems
    assert catalogue_problems(yaml.safe_load((FIX / "repo/testdata/catalogue.yaml").read_text())) == []


# --------------------------------------------------------------------------------------- profiles
def test_example_profiles_validate_and_local_fs_needs_sources(tmp_path):
    assert load_profile(PROD_PROFILE).test_authoring.mode == "git-mr"
    assert load_profile(LOCAL_PROFILE).test_authoring.sources
    with pytest.raises(Exception, match="needs sources"):
        load_profile(profile_with(tmp_path, sources=[]))
    with pytest.raises(Exception, match="format"):
        load_profile(profile_with(tmp_path, sources=[{"kind": "data", "paths": ["x/*.csv"]}]))


def test_profile_path_is_not_part_of_the_hash(profile):
    from ath_core.schemas import TestProfile
    same = TestProfile.model_validate(yaml.safe_load(LOCAL_PROFILE.read_text()))  # no source path
    assert profile.source is not None and same.source is None
    assert profile.model_dump_json() == same.model_dump_json()


def test_author_stage_is_restricted_in_the_model_policy():
    assert load_policy().data_class("author", "settlenxt") == "restricted"


# ----------------------------------------------------------------------------- CLI (fake agent CLI)
@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Harness config running the fake agent CLI in author mode; answers come from a file."""
    cfg = tmp_path / "ath.yaml"
    cfg.write_text(yaml.safe_dump({"agentRunner": {
        "command": [sys.executable, str(FAKE), "--prompt-file", "{prompt_file}", "-m", "{model}"],
        "promptVia": "file", "voters": 1,
        "passEnv": ["FAKE_AGENT_MODE", "FAKE_AUTHOR_OUTPUT", "FAKE_AGENT_RECORD"]}}))
    monkeypatch.setenv("ATH_CONFIG", str(cfg))
    monkeypatch.setenv("FAKE_AGENT_MODE", "author")
    monkeypatch.setenv("FAKE_AGENT_RECORD", str(tmp_path / "calls"))
    out = tmp_path / "answer.json"
    monkeypatch.setenv("FAKE_AUTHOR_OUTPUT", str(out))

    class Fake:
        config = cfg

        def answer(self, obj):
            out.write_text(json.dumps(obj))

        def calls(self) -> int:
            return len(list((tmp_path / "calls").glob("call-*.txt"))) if (tmp_path / "calls").exists() else 0

        def prompt(self, n=1) -> str:
            return (tmp_path / "calls" / f"call-{n}.txt").read_text()
    return Fake()


def author_cli(tmp_path, checkout, *extra):
    return main(["author", "--profile", str(LOCAL_PROFILE), "--tests", str(checkout), "--db",
                 str(tmp_path / "state.db"), "--out", str(tmp_path / "proposals"), "--user", "tester-1", *extra])


def _proposal_id(out: str) -> str:
    return out.split("proposal ", 1)[1].split(":", 1)[0]


def test_cli_author_writes_into_the_checkout(fake, tmp_path, checkout, capsys):
    fake.answer(answer(UPDATE, CREATE))
    assert author_cli(tmp_path, checkout, "--tsd", str(TSD_FILE)) == 0
    out = capsys.readouterr().out
    assert f"wrote {JAVA_TEST}" in out and (checkout / NEW_TEST).exists()
    pid = _proposal_id(out)
    folder = tmp_path / "proposals" / pid
    assert {f.name for f in folder.iterdir()} == {"proposal.json", "proposal.sha256", "changes.patch", "checklist.md"}
    kinds = [k for _, k, _ in StateStore(tmp_path / "state.db").events(pid)]
    assert kinds == ["agent.call", "author.proposed", "author.applied"]
    assert "[PAN****1111]" in fake.prompt() and "4111 1111 1111 1111" not in fake.prompt()


def test_cli_dry_run_then_apply_without_a_model_call(fake, tmp_path, checkout, capsys):
    fake.answer(answer(UPDATE))
    before = (checkout / JAVA_TEST).read_text()
    assert author_cli(tmp_path, checkout, "--tsd", str(TSD_FILE), "--dry-run") == 0
    assert (checkout / JAVA_TEST).read_text() == before
    pid = _proposal_id(capsys.readouterr().out)
    assert author_cli(tmp_path, checkout, "apply", pid) == 0
    assert "15.63" in (checkout / JAVA_TEST).read_text() and fake.calls() == 1


def test_cli_apply_refuses_a_tampered_proposal(fake, tmp_path, checkout, capsys):
    fake.answer(answer(UPDATE))
    author_cli(tmp_path, checkout, "--tsd", str(TSD_FILE), "--dry-run")
    pid = _proposal_id(capsys.readouterr().out)
    f = tmp_path / "proposals" / pid / "proposal.json"
    f.write_text(f.read_text().replace("half-up rounding", "something else"))
    assert author_cli(tmp_path, checkout, "apply", pid) == 1
    assert "recorded hash" in capsys.readouterr().out


def test_cli_refuses_uncommitted_targets(fake, tmp_path, checkout, capsys):
    fake.answer(answer(UPDATE))
    (checkout / JAVA_TEST).write_text((checkout / JAVA_TEST).read_text() + "// wip\n")
    assert author_cli(tmp_path, checkout, "--tsd", str(TSD_FILE)) == 1
    assert "uncommitted changes" in capsys.readouterr().out
    assert author_cli(tmp_path, checkout, "--tsd", str(TSD_FILE), "--force") == 0


def test_cli_refuses_a_checkout_that_is_not_a_git_repo(fake, tmp_path, capsys):
    plain = tmp_path / "plain"
    shutil.copytree(FIX / "repo", plain)
    fake.answer(answer(UPDATE))
    assert author_cli(tmp_path, plain, "--tsd", str(TSD_FILE)) == 1
    assert "not a Git repository" in capsys.readouterr().out and fake.calls() == 0  # refused before the call
    assert author_cli(tmp_path, plain, "--tsd", str(TSD_FILE), "--force") == 0


@pytest.mark.parametrize("obj, code", [
    (answer(questions=[{"clause": "AC-7", "question": "half-up or banker's rounding?"}]), 4),
    (answer({**CREATE, "clauses": ["AC-99"]}), 5),
])
def test_cli_exit_codes(fake, tmp_path, checkout, capsys, obj, code):
    fake.answer(obj)
    assert author_cli(tmp_path, checkout, "--tsd", str(TSD_FILE)) == code


@pytest.mark.parametrize("mode", ["garbage", "author-invalid"])
def test_cli_bad_model_output_is_refused(fake, tmp_path, checkout, capsys, monkeypatch, mode):
    monkeypatch.setenv("FAKE_AGENT_MODE", mode)
    assert author_cli(tmp_path, checkout, "--tsd", str(TSD_FILE)) == 1
    assert "refused" in capsys.readouterr().out


def test_cli_refuses_a_forbidden_author_model_before_any_call(fake, tmp_path, checkout, capsys):
    data = yaml.safe_load(LOCAL_PROFILE.read_text())
    data["models"] = {"default": "local/onprem-coder", "author": "openai/gpt-4o"}
    path = LOCAL_PROFILE.parent / f".tmp-profile-{tmp_path.name}.yaml"
    path.write_text(yaml.safe_dump(data))
    fake.answer(answer(UPDATE))
    rc = main(["author", "--profile", str(path), "--tests", str(checkout), "--tsd", str(TSD_FILE),
               "--db", str(tmp_path / "db"), "--out", str(tmp_path / "p")])
    assert rc == 1 and "model policy" in capsys.readouterr().out and fake.calls() == 0


def test_cli_from_run_sends_outdated_tests_and_reuses_the_run_inputs(fake, tmp_path, checkout, capsys):
    store = StateStore(tmp_path / "state.db")
    orch = Orchestrator(load_profile(LOCAL_PROFILE), store, report_dir=tmp_path)
    ctx = RunContext(run_id="r-old", product="settlenxt", commit="mr001", merge_base="base000", env="uat1",
                     triggered_by="dev-asha")
    orch.start(ctx, context={"tsd": TSD, "diff": "diff --git a/clearing/Fee.java b/clearing/Fee.java\n"
                                                  "--- a/clearing/Fee.java\n+++ b/clearing/Fee.java\n@@ -1 +1 @@\n"
                                                  "-HALF_DOWN\n+HALF_UP rounding\n"})
    run = ATRFRun.model_validate_json(store.get_artifact("r-old", "atrf/smoke.json"))
    case = run.failing()[0]
    case.triage = Triage(category=TriageCategory.TEST_OUTDATED, decided_by="vote")
    store.put_artifact("r-old", "atrf/smoke.json", run.model_dump_json())
    fake.answer(answer(UPDATE))
    assert author_cli(tmp_path, checkout, "--from-run", "r-old", "--dry-run") == 0
    prompt = fake.prompt()
    outdated = prompt.rsplit('<data name="outdated">', 1)[1].split("</data>", 1)[0]  # the skill names it too
    assert case.id in outdated and "HALF_UP rounding" in prompt and "ⓘ R-3 · AC-7" in prompt


def test_doctor_checks_authoring(tmp_path, checkout, capsys):
    assert main(["doctor", str(LOCAL_PROFILE), "--tests", str(checkout)]) == 0
    out = capsys.readouterr().out
    assert "[ok] product skill" in out and "[ok] test authoring (T3): mode local-fs" in out
    assert "testData catalogue: 5 entries" in out
    (checkout / "testdata/catalogue.yaml").write_text(
        "entries:\n  - {name: bad, kind: card, values: {pan: '4111111111111112'}}\n")
    assert main(["doctor", str(LOCAL_PROFILE), "--tests", str(checkout)]) == 2
    assert "Luhn" in capsys.readouterr().out


# ------------------------------------------------------------------------------------- T3 in a run
def _ctx(checkout, run_id="a1", **kw):
    base = dict(run_id=run_id, product="settlenxt", commit="mr001", merge_base="base000", env="uat1",
                mode=RunMode.ASSIST, triggered_by="dev-asha", tests_dir=str(checkout))
    base.update(kw)
    return RunContext(**base)


def _run_orch(profile, stub, tmp_path, store=None):
    return Orchestrator(profile, store or StateStore(":memory:"), report_dir=tmp_path, author_factory=lambda: stub,
                        profile_ref=str(LOCAL_PROFILE))


def test_run_parks_at_t3_then_applies_the_approved_proposal(profile, checkout, tmp_path):
    stub = StubAgent(answer(UPDATE, CREATE, questions=[{"clause": "AC-8", "question": "partial refunds?"}]))
    orch = _run_orch(profile, stub, tmp_path)
    m = orch.start(_ctx(checkout), context={"tsd": TSD})
    assert m.status == RunStatus.WAITING_GATE and m.stages[Stage.T3_AUTHOR].status == StageStatus.WAITING_GATE
    h = Proposal.model_validate_json(orch.store.get_artifact("a1", "author/proposal.json")).hash
    assert h in m.stages[Stage.T3_AUTHOR].output["gate"]
    assert orch.store.get_artifact("a1", "author/changes.patch") and not (checkout / NEW_TEST).exists()
    m = orch.resume("a1")  # resume before approval reuses the stored proposal
    assert m.status == RunStatus.WAITING_GATE and stub.calls == 1
    with pytest.raises(ValueError, match="is not the stored T3 proposal"):
        orch.approve("a1", Stage.T3_AUTHOR, "lead-ravi", OWNERS, ref="deadbeef")
    m = orch.approve("a1", Stage.T3_AUTHOR, "lead-ravi", OWNERS, ref=h)
    assert m.status == RunStatus.SUCCEEDED and stub.calls == 1
    assert m.gates[0].ref == h and (checkout / NEW_TEST).exists()
    assert m.stages[Stage.T3_AUTHOR].output["applied"] == [JAVA_TEST, NEW_TEST]
    report = orch.store.get_artifact("a1", "report.md")
    assert "### Test authoring (T3)" in report and "approved by lead-ravi" in report and "partial refunds?" in report
    kinds = [k for _, k, _ in orch.store.events("a1")]
    assert kinds.count("agent.call") == 1 and "author.applied" in kinds


def test_run_fails_t3_when_the_proposal_changed_after_approval(profile, checkout, tmp_path):
    orch = _run_orch(profile, StubAgent(answer(UPDATE)), tmp_path)
    orch.start(_ctx(checkout), context={"tsd": TSD})
    orch.record_gate("a1", Stage.T3_AUTHOR, "approved", "lead-ravi", OWNERS)
    body = json.loads(orch.store.get_artifact("a1", "author/proposal.json"))
    body["summary"] = "swapped after approval"
    orch.store.put_artifact("a1", "author/proposal.json", json.dumps(body))
    m = orch.resume("a1")
    assert m.stages[Stage.T3_AUTHOR].status == StageStatus.FAILED
    assert "proposal changed after approval" in m.stages[Stage.T3_AUTHOR].error
    assert "15.63" not in (checkout / JAVA_TEST).read_text()


def test_run_with_nothing_to_apply_needs_no_gate(profile, checkout, tmp_path):
    orch = _run_orch(profile, StubAgent(answer(questions=[{"clause": "AC-7", "question": "q?"}])), tmp_path)
    m = orch.start(_ctx(checkout), context={"tsd": TSD})
    assert m.status == RunStatus.SUCCEEDED and m.stages[Stage.T3_AUTHOR].status == StageStatus.DONE
    assert "nothing to approve" in m.stages[Stage.T3_AUTHOR].output["reason"]


@pytest.mark.parametrize("ctx_kw, authoring, context, reason", [
    ({"author": False}, {}, {"tsd": TSD}, "--no-author"),
    ({}, {"enabled": False}, {"tsd": TSD}, "enabled: false"),
    ({}, {"mode": "git-mr"}, {"tsd": TSD}, "mode git-mr is not implemented yet"),
    ({"mode": RunMode.REPORT_ONLY}, {}, {"tsd": TSD}, "report-only"),
    ({}, {}, {}, "no TSD given"),
    ({"tests_dir": None}, {}, {"tsd": TSD}, "no test checkout given"),
])
def test_t3_is_skipped_with_its_reason(checkout, tmp_path, ctx_kw, authoring, context, reason):
    prof = load_profile(profile_with(tmp_path, **authoring)) if authoring else load_profile(LOCAL_PROFILE)
    stub = StubAgent(answer(UPDATE))
    m = _run_orch(prof, stub, tmp_path).start(_ctx(checkout, **ctx_kw), context=context)
    t3 = m.stages[Stage.T3_AUTHOR]
    assert t3.status == StageStatus.SKIPPED and reason in t3.output["reason"] and stub.calls == 0


def test_author_flag_overrides_a_disabled_profile(checkout, tmp_path):
    prof = load_profile(profile_with(tmp_path, enabled=False))
    m = _run_orch(prof, StubAgent(answer(UPDATE)), tmp_path).start(_ctx(checkout, author=True), context={"tsd": TSD})
    assert m.stages[Stage.T3_AUTHOR].status == StageStatus.WAITING_GATE


def test_forbidden_author_model_fails_t3_before_any_call(profile, checkout, tmp_path):
    def factory():
        raise ValueError("model policy: stage 'author' sees 'restricted' data")
    orch = Orchestrator(profile, StateStore(":memory:"), report_dir=tmp_path, author_factory=factory)
    m = orch.start(_ctx(checkout), context={"tsd": TSD})
    assert m.stages[Stage.T3_AUTHOR].status == StageStatus.FAILED and "model policy" in m.stages[Stage.T3_AUTHOR].error


def test_product_skill_reaches_triage_voters(profile):
    seen = []

    class Voter:
        name = "v"

        def classify(self, case, evidence):
            seen.append(evidence.get("product", ""))
            return TriageCategory.UNKNOWN

    orch = Orchestrator(profile, StateStore(":memory:"), agents=[Voter()])
    orch.start(RunContext(run_id="p1", product="settlenxt", commit="mr001", merge_base="base000", env="uat1",
                          triggered_by="dev-asha"))
    assert seen and all("SettleNxt product skill" in s for s in seen)


def test_cli_run_with_tests_and_approve_ref(fake, tmp_path, checkout, capsys, monkeypatch):
    fake.answer(answer(UPDATE))
    common = ["--profile", str(LOCAL_PROFILE), "--db", str(tmp_path / "s.db"), "--report-dir", str(tmp_path / "r")]
    assert main(["run", *common, "--commit", "mr001", "--merge-base", "base000", "--env", "uat1", "--mode", "assist",
                 "--user", "dev-asha", "--run-id", "c1", "--tsd", str(TSD_FILE), "--tests", str(checkout),
                 "--quiet"]) == 0
    assert "T3   waiting-gate" in capsys.readouterr().out
    assert main(["approve", "c1", *common, "--gate", "T3", "--user", "lead-ravi", "--groups", "settlement-dept",
                 "--ref", "0" * 64]) == 1
    assert "not the stored T3 proposal" in capsys.readouterr().out
    h = Proposal.model_validate_json(StateStore(tmp_path / "s.db").get_artifact("c1", "author/proposal.json")).hash
    monkeypatch.setenv("FAKE_AGENT_MODE", "answer")  # triage voters after T3 get the triage answer
    assert main(["approve", "c1", *common, "--gate", "T3", "--user", "lead-ravi", "--groups", "settlement-dept",
                 "--ref", h]) == 0
    assert "15.63" in (checkout / JAVA_TEST).read_text()
