"""Review flags for model-written test changes. They never drop a change; they go in the checklist.

Model-written tests run in CI with CI credentials, and the TSD, diff, code and tests the model read can
carry prompt injection. So the reviewer is pointed at changes that weaken tests or reach outside them.
"""
from __future__ import annotations

import difflib
import re

# Lines that check something: JUnit/TestNG/AssertJ/Hamcrest/Mockito, pytest, Jest/Chai, Cucumber steps.
ASSERTION = re.compile(r"\b(assert\w*|verify\w*|expect\w*|fail)\s*\(|\bassert\s|\.should\w*\b|"
                       r"\.(?:isEqualTo|isTrue|isFalse|isNull|isNotNull|contains\w*|has\w+|matches)\s*\(|"
                       r"^\s*Then\b", re.M)
TEST_METHOD = re.compile(r"@Test\b|@ParameterizedTest\b|^\s*def test_|^\s*(?:it|test)\s*\(|^\s*Scenario", re.M)
DISABLE = re.compile(r"@Disabled\b|@Ignore\b|\bassume\w*\s*\(|\bAssumptions\.|@Tag\(\s*\"skip|"
                     r"pytest\.mark\.skip|pytest\.skip\(|\.skip\(|\bxit\(|\bxdescribe\(|@skip\b|@wip\b|"
                     r"enabled\s*=\s*false", re.I)
TOLERANCE = re.compile(r"\btimeout\w*\b|\btolerance\b|\bdelta\b|\bepsilon\b|\bwithin\s*\(|\boffset\s*\(|"
                       r"\bcloseTo\s*\(|\bassertThrows\b|\bexpected\s*=|ExpectedException|expectedExceptions|"
                       r"Duration\.of|Thread\.sleep|TimeUnit\.|\bpytest\.raises\b|\bapprox\s*\(", re.I)
IMPORT = re.compile(r"^\s*(?:import\s|from\s+\S+\s+import\s|#include\b|using\s+[\w.]+\s*;|require\s*\()")
# Network, process, file system, reflection and environment access.
RISKY_APIS = (
    "java.net", "HttpClient", "HttpURLConnection", "URLConnection", "new URL(", "Socket", "RestTemplate",
    "WebClient", "OkHttp", "Runtime.getRuntime", "Runtime.exec", ".exec(", "ProcessBuilder", "java.nio.file",
    "java.io.File", "new File(", "Files.", "FileOutputStream", "FileWriter", "System.getenv", "System.getProperty",
    "System.setProperty", "System.exit", "Class.forName", "setAccessible", "getDeclaredMethod", "getDeclaredField",
    "java.lang.reflect", "ScriptEngine", "javax.script", "ObjectInputStream",
    "subprocess", "os.system", "os.environ", "requests.", "urllib", "socket.", "eval(", "exec(",
    "child_process", "process.env", "fetch(",
)


def _diff_lines(old: str, new: str) -> tuple[list[str], list[str]]:
    removed, added = [], []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        (removed if line.startswith("-") else added).append(line[1:])
    return removed, added


def _count(regex: re.Pattern[str], lines: list[str]) -> int:
    return sum(1 for line in lines if regex.search(line))


def review_flags(old: str, new: str, kind: str = "code") -> list[str]:
    """Flags for one file change (old is "" for a new file). Works on masked text."""
    removed, added = _diff_lines(old, new)
    flags = []
    if kind == "data":
        if removed and not added:
            flags.append(f"removes {len(removed)} line(s) of test data without adding any")
        return flags
    rm_assert, add_assert = _count(ASSERTION, removed), _count(ASSERTION, added)
    if rm_assert > add_assert:
        flags.append(f"weakens checks: {rm_assert} assertion line(s) removed, {add_assert} added")
    rm_tests, add_tests = _count(TEST_METHOD, removed), _count(TEST_METHOD, added)
    if rm_tests > add_tests:
        flags.append(f"removes {rm_tests - add_tests} test method(s)")
    if dis := [line.strip() for line in added if DISABLE.search(line)]:
        flags.append(f"disables or skips tests: {'; '.join(dis[:3])}")
    # Only for an existing file: in a new one every timeout or assertThrows would be flagged.
    if old and (tol := [line.strip() for line in removed + added if TOLERANCE.search(line)]):
        flags.append(f"changes tolerances, timeouts or expected exceptions: {'; '.join(tol[:3])}")
    if imports := [line.strip() for line in added if IMPORT.search(line)]:
        flags.append(f"new imports: {'; '.join(imports[:5])}" + (f" (+{len(imports) - 5} more)" if len(imports) > 5 else ""))
    risky = sorted({api for line in added for api in RISKY_APIS if api in line})
    if risky:
        flags.append(f"calls network, process, file-system, reflection or environment APIs: {', '.join(risky)}")
    return flags


def assertion_lines(old: str, new: str, limit: int = 8) -> list[str]:
    """Added assertion lines, for the checklist."""
    _, added = _diff_lines(old, new)
    return [line.strip() for line in added if ASSERTION.search(line)][:limit]
