"""Stand-in for a headless agent CLI (AiNxt CLI, grok-cli style) in tests.

Mirrors the AiNxt CLI's headless interface (unknown flags such as --sandbox are ignored):
  fake_agent_cli.py [-p PROMPT | --prompt-file PATH] [-m MODEL] [--cwd DIR] [--output-format plain|json]
Without -p or --prompt-file it reads stdin, like simpler grok-style CLIs.
Behaviour is chosen by $FAKE_AGENT_MODE, which the test passes through `passEnv`:
  answer   chatter, then a fenced JSON answer (code-regression if the case mentions SNX-001)
  garbage  no JSON at all
  invalid  JSON with a category outside the schema
  fail     exit code 3
  sleep    sleep longer than the test timeout
  env      answer, with the names of the environment variables it received as evidence
  echo     answer, with the prompt itself as evidence
  error    `ainxt --output-format json` error envelope, exit 1
  invent   votes with a quote that is not in the inputs
  noquote  votes with an empty quote
  skill    votes quoting the skill's own rules instead of the data
  spawn    starts a child that sleeps, records both pids in $FAKE_AGENT_PIDS, then hangs
  hang     hangs unless resumed with -r; the resumed call answers (and reports its session id)
  silent   prints nothing for 3 s, then answers
  drip     prints a line every 0.3 s for 1.5 s, then answers
The `quote` is the first log line, else the failure message, copied from the prompt.
"""
import argparse
import json
import os
import re
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("-p")
ap.add_argument("--prompt-file")
ap.add_argument("-m", default="")
ap.add_argument("--cwd")
ap.add_argument("--output-format", default="plain")
ap.add_argument("-r", "--resume")
ap.add_argument("--session-id")
a, _unknown = ap.parse_known_args()
prompt = a.p if a.p is not None else open(a.prompt_file).read() if a.prompt_file else sys.stdin.read()
mode = os.environ.get("FAKE_AGENT_MODE", "answer")
as_json = a.output_format == "json"

if mode == "spawn":
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.environ["FAKE_AGENT_PIDS"], "w") as f:
        f.write(f"{os.getpid()} {child.pid}")
    time.sleep(60)
if mode == "hang" and not a.resume:
    print(f"started session {a.session_id}", flush=True)
    time.sleep(60)
if mode == "silent":
    time.sleep(3)
if mode == "drip":
    for i in range(5):
        print(f"working {i}", flush=True)
        time.sleep(0.3)

if mode == "error":
    print(json.dumps({"type": "error", "message": "No models are available"}) if as_json else "No models are available")
    sys.exit(1)

if mode == "fail":
    print("model gateway unreachable", file=sys.stderr)
    sys.exit(3)
if mode == "sleep":
    time.sleep(30)
if mode == "garbage":
    print("I could not decide.")
    sys.exit(0)
if mode == "invalid":
    print(json.dumps({"category": "definitely-a-bug", "evidence": "x"}))
    sys.exit(0)

category = "code-regression" if '"SNX-001"' in prompt else "environment"
logs = re.search(r'<data name="logs">\n(.*?)\n</data>', prompt, re.S)
case = re.search(r'<data name="case">\n(.*?)\n</data>', prompt, re.S)
try:  # read the failure message the way a model would: as text, not as escaped JSON
    message = (json.loads(case.group(1)).get("failure") or {}).get("message", "") if case else ""
except ValueError:
    message = ""
quote = logs.group(1).splitlines()[0] if logs and logs.group(1).strip() else message
quote = {"invent": "NullPointerException at Settlement.java:42", "noquote": "",
         "skill": "Mandatory rules for every ATH stage skill"}.get(mode, quote)
env_view = ",".join(f"{k}={v}" if k.startswith("AINXT_") else k for k, v in sorted(os.environ.items()))
evidence = {"env": env_view, "echo": prompt, "hang": f"resumed={a.resume}"}.get(
    mode, f"model={a.m}; cwd={a.cwd or os.getcwd()}")
# Models print reasoning before the answer.
text = "Thinking about the failure...\nReading the logs.\n```json\n" + json.dumps(
    {"category": category, "quote": quote, "evidence": evidence}) + "\n```"
if as_json:  # ainxt --output-format json: one envelope, answer in "text"
    print(json.dumps({"text": text, "stopReason": "end_turn", "sessionId": "s-1", "requestId": "r-1", "num_turns": 1}))
else:
    print(text)
