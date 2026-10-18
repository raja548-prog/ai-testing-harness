"""Run LLM stages through a headless agent CLI (the AiNxt CLI, or any grok-cli style tool).

One stage call = one short-lived CLI session:
  1. build a prompt from the stage skill (SKILL.md + includes) and <data> blocks
  2. start the CLI in an empty temporary directory with a minimal environment
  3. take the last JSON object in its output that matches the skill's output_schema

The CLI only ever receives masked data, and ATH never acts on its text beyond that JSON.
It must not push code or call Jenkins, Jira or UAT itself: side effects go through ATH.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ath_core.config import AgentRunnerConfig, HarnessConfig
from ath_core.masking import Masker
from ath_core.policy import load_policy
from ath_core.schemas.atrf import ATRFCase, TriageCategory
from ath_core.schemas.profile import TestProfile
from ath_core.triage.voting import Vote

BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
_INCLUDE = re.compile(r"^Include:\s*`([^`]+)`\s*$", re.MULTILINE)


class AgentError(RuntimeError):
    """The CLI failed, timed out, or returned no usable JSON. Votes count it as `unknown`."""


@dataclass
class Skill:
    name: str
    text: str
    output_schema: dict[str, str]


def load_skill(path: str | Path) -> Skill:
    """Load SKILL.md: YAML front matter, body, and inlined `Include:` files."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = raw
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        meta = yaml.safe_load(front) or {}
    body = _INCLUDE.sub(lambda m: (path.parent / m.group(1)).read_text(encoding="utf-8"), body)
    schema = meta.get("output_schema") or {}
    if isinstance(schema, str):
        schema = json.loads(schema)
    return Skill(name=meta.get("name", path.parent.name), text=body.strip(), output_schema=schema)


def _escape(value: str) -> str:
    # Data must not be able to close its own <data> block and pose as instructions.
    return value.replace("</data", "<\\/data")


def build_prompt(skill: Skill, data: dict[str, str]) -> str:
    blocks = "\n\n".join(f'<data name="{k}">\n{_escape(v)}\n</data>' for k, v in data.items())
    return (f"{skill.text}\n\n{blocks}\n\n"
            f"Respond with exactly one JSON object with these keys and value rules, and nothing else:\n"
            f"{json.dumps(skill.output_schema)}\n")


def validate_output(obj: Any, schema: dict[str, str]) -> bool:
    """Every schema key present; `a|b|c` values must be one of the listed options."""
    if not isinstance(obj, dict):
        return False
    for key, rule in schema.items():
        if key not in obj:
            return False
        if isinstance(rule, str) and "|" in rule and obj[key] not in rule.split("|"):
            return False
    return True


def _envelope(text: str) -> dict[str, Any] | None:
    """The CLI's own JSON envelope (e.g. `ainxt --output-format json`), if stdout is one."""
    try:
        obj = json.loads(text.strip())
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def extract_json(text: str, schema: dict[str, str]) -> dict[str, Any]:
    """The last JSON object in the CLI output that satisfies the schema (CLIs print chatter too).

    Plain output is scanned as is. A JSON envelope such as {"text": "...", "stopReason": ...}
    is unwrapped first; {"type": "error", "message": ...} becomes an AgentError.
    """
    env = _envelope(text)
    if env is not None and not validate_output(env, schema):
        if env.get("type") == "error":
            raise AgentError(f"agent CLI error: {str(env.get('message', ''))[:300]}")
        if isinstance(env.get("text"), str):
            text = env["text"]
    decoder = json.JSONDecoder()
    found = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except ValueError:
            continue
        if validate_output(obj, schema):
            found = obj
    if found is None:
        raise AgentError("agent output contains no JSON object matching the stage schema")
    return found


CONTINUE_PROMPT = ("Continue from where you stopped and finish now. Respond with exactly one JSON object "
                   "with these keys and value rules, and nothing else:\n{schema}\n")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop the CLI and everything it started (tool shells, subagents), not just the parent."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)  # the CLI leads its own process group (start_new_session)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5)
            if sig == signal.SIGTERM:
                os.killpg(proc.pid, signal.SIGKILL)  # children that ignored SIGTERM or outlived the parent
        except subprocess.TimeoutExpired:
            continue
        except (ProcessLookupError, PermissionError):
            return
        return


@dataclass
class _Attempt:
    stdout: str
    stderr: str
    returncode: int | None
    stopped: str = ""  # "timeout" | "idle" when ATH killed the session


class CliAgentRunner:
    """Runs one stage call as a headless CLI session.

    - timeoutSec caps each attempt; idleTimeoutSec (0 = off) stops a session that prints nothing for
      that long. Either way the whole process tree is killed.
    - With sessionArgs/resumeArgs, ATH picks the session id up front and, after a timeout, resumes the
      same session (context kept) up to timeoutResumes times with a short "finish now" prompt.
    - `last_call` records the audit facts of the latest call: model, session, attempts, elapsed time,
      exit code, and SHA-256 of the prompt and of the raw response (never their text).
    """

    def __init__(self, cfg: AgentRunnerConfig):
        self.cfg = cfg
        self.last_call: dict[str, Any] = {}

    def _env(self) -> dict[str, str]:
        # Only what the CLI needs to start and reach the model gateway; no harness secrets.
        names = list(BASE_ENV) + list(self.cfg.pass_env)
        return {**{n: os.environ[n] for n in names if n in os.environ}, **self.cfg.set_env}

    def run(self, skill: Skill, data: dict[str, str], model: str = "") -> dict[str, Any]:
        prompt = build_prompt(skill, data)
        session_id = str(uuid.uuid4()) if (self.cfg.session_args or self.cfg.resume_args) else ""
        started = time.monotonic()
        call: dict[str, Any] = {"model": model, "session_id": session_id, "prompt_sha256": _sha256(prompt)}
        self.last_call = call
        att = self._attempt(prompt, model, session_id, resume=False)
        resumes = 0
        while att.stopped and session_id and resumes < self.cfg.timeout_resumes:
            resumes += 1
            att = self._attempt(CONTINUE_PROMPT.format(schema=json.dumps(skill.output_schema)), model,
                                session_id, resume=True)
        call.update(attempts=1 + resumes, elapsed_s=round(time.monotonic() - started, 2),
                    exit_code=att.returncode, stopped=att.stopped or None, response_sha256=_sha256(att.stdout))
        if att.stopped:
            limit = self.cfg.timeout_sec if att.stopped == "timeout" else self.cfg.idle_timeout_sec
            what = "timed out" if att.stopped == "timeout" else "printed nothing"
            raise AgentError(f"agent CLI {what} for {limit}s and was stopped"
                             + (f" after {resumes} resume(s)" if resumes else ""))
        if att.returncode != 0:
            env = _envelope(att.stdout) or {}
            detail = env.get("message") if env.get("type") == "error" else att.stderr.strip()[-300:]
            raise AgentError(f"agent CLI exited with {att.returncode}: {detail}")
        return extract_json(att.stdout, skill.output_schema)

    def _command(self, prompt: str, prompt_file: Path, workdir: str, model: str, session_id: str,
                 resume: bool) -> list[str]:
        values = {"{model}": model, "{workdir}": workdir, "{prompt_file}": str(prompt_file),
                  "{session_id}": session_id}
        extra = (self.cfg.resume_args if resume else self.cfg.session_args) if session_id else []
        cmd = []
        for arg in [*self.cfg.command, *extra]:
            for k, v in values.items():
                arg = arg.replace(k, v)
            cmd.append(arg.replace("{prompt}", prompt) if self.cfg.prompt_via == "arg" else arg)
        return cmd

    def _attempt(self, prompt: str, model: str, session_id: str, resume: bool) -> _Attempt:
        with tempfile.TemporaryDirectory(prefix="ath-agent-") as workdir:
            prompt_file = Path(workdir) / "prompt.md"
            if self.cfg.prompt_via == "file":
                prompt_file.write_text(prompt, encoding="utf-8")
            cmd = self._command(prompt, prompt_file, workdir, model, session_id, resume)
            group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                     else {"start_new_session": True})
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE if self.cfg.prompt_via == "stdin" else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    errors="replace", cwd=workdir, env=self._env(), **group)
            except FileNotFoundError:
                raise AgentError(f"agent CLI {cmd[0]!r} not found on PATH")
            out: list[str] = []
            err: list[str] = []
            last = [time.monotonic()]

            def pump(stream, buf: list[str]) -> None:
                for line in stream:
                    buf.append(line)
                    last[0] = time.monotonic()

            readers = [threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
                       threading.Thread(target=pump, args=(proc.stderr, err), daemon=True)]
            for t in readers:
                t.start()
            if self.cfg.prompt_via == "stdin":
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            start, stopped = time.monotonic(), ""
            while proc.poll() is None:
                now = time.monotonic()
                if now - start >= self.cfg.timeout_sec:
                    stopped = "timeout"
                elif self.cfg.idle_timeout_sec and now - last[0] >= self.cfg.idle_timeout_sec:
                    stopped = "idle"
                if stopped:
                    _kill_tree(proc)
                    break
                time.sleep(0.05)
            if not stopped and os.name != "nt":
                try:  # tools the CLI started and left running must not outlive the session
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            proc.wait()
            for t in readers:
                t.join(timeout=5)
        return _Attempt("".join(out), "".join(err), proc.returncode, stopped)


# A quote shorter than this proves little (e.g. "0.00" or a test id appears in many places).
MIN_QUOTE_CHARS = 12
_PLACEHOLDER = "(not attached)"


def _norm(text: str) -> str:
    return " ".join(text.split())


def _clean_quote(quote: str) -> str:
    q = _norm(quote).strip("`\"'")
    return q[:-3].rstrip() if q.endswith("...") else q


def quote_found(quote: str, sources: list[str]) -> bool:
    """True when the quote appears, whitespace-insensitively, in one of the data sources ATH sent."""
    q = _clean_quote(quote)
    return len(q) >= MIN_QUOTE_CHARS and any(q in _norm(src) for src in sources)


class CliTriageAgent:
    """One T7 voter backed by a CLI session running skills/t07-triage.

    A vote counts only if its `quote` is found in the data ATH sent (not in the skill text);
    otherwise it becomes `unknown` with a note. This catches invented evidence without a model.
    """

    def __init__(self, name: str, runner: CliAgentRunner, skill: Skill, model: str, masker: Masker):
        self.name, self.runner, self.skill, self.model, self.masker = name, runner, skill, model, masker
        self.last_evidence: str = ""
        self.last_call: dict[str, Any] = {}  # audit record of this voter's latest CLI call

    def classify(self, case: ATRFCase, evidence: dict[str, Any]) -> Vote:
        data = {
            "case": case.model_dump_json(exclude={"triage"}, indent=1),
            "logs": "\n".join(evidence.get("logs") or []),
            "diff": str(evidence.get("diff") or f"{_PLACEHOLDER} commit {evidence.get('diff_ref', '?')}"),
            "tsd": str(evidence.get("tsd") or _PLACEHOLDER),
            "product": str(evidence.get("product") or _PLACEHOLDER),
        }
        # Inputs are masked upstream; mask again before anything leaves the harness.
        sent = {k: self.masker.mask(v) for k, v in data.items()}
        try:
            out = self.runner.run(self.skill, sent, self.model)
        finally:  # voters share one runner; keep our own copy of the call record, failed calls too
            self.last_call = dict(self.runner.last_call)
        category = TriageCategory(out["category"])
        self.last_evidence = str(out.get("evidence", ""))
        quote = self.masker.mask(str(out.get("quote") or ""))
        if category == TriageCategory.UNKNOWN:
            return Vote(category, note=f"{self.name}: unknown ({self.last_evidence[:150] or 'no reason given'})")
        # What the model could have quoted: every real data block, plus the case's text fields unescaped
        # (the case block is JSON, so a quoted failure message would not match it character for character).
        sources = [v for v in sent.values() if not v.startswith(_PLACEHOLDER)]
        sources += [self.masker.mask(t) for t in (case.name, case.failure.message if case.failure else "",
                                                  case.failure.trace if case.failure else "") if t]
        if not quote_found(quote, sources):
            why = (f"gave no quote of at least {MIN_QUOTE_CHARS} characters"
                   if len(_clean_quote(quote)) < MIN_QUOTE_CHARS else "quoted text that is not in the inputs")
            return Vote(TriageCategory.UNKNOWN, note=f"{self.name}: voted {category.value} but {why}; vote discarded")
        return Vote(category, evidence=_clean_quote(quote))


def model_family(model: str, families: dict[str, str]) -> str | None:
    for pattern, family in families.items():
        if fnmatch.fnmatchcase(model, pattern):
            return family
    return None


def voter_models(cfg: HarnessConfig, profile: TestProfile) -> list[str]:
    rc = cfg.agent_runner
    if rc is None:
        return []
    models = rc.models or [profile.models.for_stage("triage") if profile.models else ""]
    return [models[i % len(models)] for i in range(rc.voters)]


def diversity_problem(cfg: HarnessConfig, models: list[str]) -> str | None:
    """Why the voters do not meet minFamilies, or None. Unclassified models fail closed."""
    rc = cfg.agent_runner
    if rc is None or rc.min_families <= 1:
        return None
    unclassified = sorted({m or "(no model)" for m in models if model_family(m, rc.model_families) is None})
    if unclassified:
        return f"minFamilies is {rc.min_families} but these models match no modelFamilies pattern: {unclassified}"
    found = sorted({model_family(m, rc.model_families) for m in models})
    if len(found) < rc.min_families:
        return (f"triage voters come from {len(found)} model family {found}; minFamilies needs "
                f"{rc.min_families}: list models from different families under agentRunner.models")
    return None


def build_triage_agents(cfg: HarnessConfig, profile: TestProfile) -> list[CliTriageAgent]:
    """Voters for T7 from the harness config; none when no agent runner is configured.

    Raises ValueError when the voters break the model-family rule or the model policy.
    """
    rc = cfg.agent_runner
    if rc is None:
        return []
    models = voter_models(cfg, profile)
    if problem := diversity_problem(cfg, models):
        raise ValueError(problem)
    if problem := load_policy(cfg.model_policy).problem(models, "triage", profile.product):
        raise ValueError(problem)
    skill = load_skill(cfg.skills_dir / "t07-triage" / "SKILL.md")
    runner = CliAgentRunner(rc)
    masker = Masker(categories=profile.logs.masking, correlation_keys=profile.correlation_keys())
    return [CliTriageAgent(f"cli-triage-{i + 1}", runner, skill, m, masker) for i, m in enumerate(models)]
