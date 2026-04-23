"""Auto-fix agent for failed GitHub Actions workflow runs.

Triggered by .github/workflows/autofix-on-failure.yml on any SANG pipeline
workflow failure. Fetches the failed run's logs, asks Claude to classify
and propose a minimal patch, applies it if safe, and opens a PR.

Environment (all required, injected by the workflow):
  ANTHROPIC_API_KEY  Claude API key
  GH_TOKEN           GitHub token for gh CLI
  RUN_ID             Failed workflow run ID
  WORKFLOW_NAME      Workflow display name (e.g. "Hiking Daily Build")
  HEAD_SHA           Commit SHA that failed
  RUN_URL            HTML URL of the failed run

Exit codes:
  0  PR opened OR deliberately skipped (skip is not a failure)
  1  Unrecoverable error in autofix itself
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import anthropic

MODEL_ID = "claude-sonnet-4-6"
MAX_LOG_LINES = 500
MAX_TREE_CHARS = 8000
MAX_CLAUDEMD_CHARS = 12000
BRANCH_PREFIX = "autofix/"


def sh(cmd: list[str], check: bool = True, capture: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=capture, text=True, check=check)
    return r.stdout if capture else ""


def fetch_failed_logs(run_id: str) -> str:
    try:
        out = sh(["gh", "run", "view", run_id, "--log-failed"], check=False)
    except subprocess.CalledProcessError as e:
        return f"(log fetch failed: {e})"
    lines = out.splitlines()
    if len(lines) > MAX_LOG_LINES:
        lines = [f"... (truncated to last {MAX_LOG_LINES} lines) ...", *lines[-MAX_LOG_LINES:]]
    return "\n".join(lines)


def recent_commits() -> str:
    return sh(["git", "log", "-20", "--oneline"]).strip()


def repo_tree() -> str:
    return sh(["git", "ls-tree", "-r", "--name-only", "HEAD"])[:MAX_TREE_CHARS]


def load_claude_md() -> str:
    p = Path("CLAUDE.md")
    return p.read_text()[:MAX_CLAUDEMD_CHARS] if p.exists() else ""


PROMPT = """You are the autofix agent for the Sports & Nature Gear content pipeline.
A GitHub Actions workflow just failed. Diagnose, decide if a safe auto-fix
is possible, and if so propose a MINIMAL patch.

## Failed workflow
Workflow: {workflow}
Run URL: {run_url}
Head commit: {head_sha}

## Recent commits (newest first)
{commits}

## Repo file tree (truncated)
{tree}

## Failure logs (tail, truncated)
```
{logs}
```

## CLAUDE.md (authoritative rules — you must respect these)
{claude_md}

## Decision protocol

Respond with ONLY a JSON object. No prose, no markdown fences.

Schema:
{{
  "decision": "patch" | "skip",
  "classification": "a" | "b" | "c" | "d" | "e" | "f",
  "root_cause": "<1-2 sentence plain-English explanation>",
  "commit_message": "fix: <short summary>",
  "patches": [
    {{"file": "<repo-relative path>", "find": "<exact current text>", "replace": "<new text>"}}
  ],
  "skip_reason": "<only required when decision=skip>"
}}

Classifications:
  a  code bug (Python, import, type, typo, missing null-check)
  b  config drift (YAML issue, cron syntax, env-var reference)
  c  external API (Amazon / GeniusLink / Anthropic / Airtable 4xx/5xx)
  d  secrets missing or invalid
  e  data/runtime (0 products after filter, LLM invented product, row-count mismatch)
  f  infra (runner quota, network)

Auto-fix policy — PATCH only when ALL of these hold:
  - Classification is (a) or (b)
  - The fix is small, obvious, localized (typo, max_tokens bump, null-check,
    cron syntax, YAML key typo, etc.)
  - You are >=90% confident it addresses the root cause
  - It matches existing patterns already in the codebase

SKIP (decision=skip) whenever any of these are true:
  - Classification is (c), (d), (e), or (f)
  - The change would touch secrets (never)
  - The change would touch the Heat Score formula in any ranker.py (explicitly
    forbidden by CLAUDE.md)
  - The change would touch models.py (breaking schema changes need Airtable updates)
  - The change would rewrite an LLM prompt in any content_generator.py
  - The change would rewrite Airtable client / upsert logic
  - It requires new tests, new fixtures, or architectural changes
  - The same file was recently modified in a way that suggests in-progress work

Patch rules:
  - Each `find` string must appear EXACTLY ONCE in the target file
  - Keep patches minimal — prefer 1-3 line diffs
  - Do not add comments, do not reformat surrounding code
  - Do not invent new imports unless strictly necessary for the fix

If you cannot satisfy all conditions above, return decision=skip with a
specific skip_reason — that is the correct, safe answer.
"""


def ask_claude(**fmt) -> dict:
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL_ID,
        max_tokens=4096,
        messages=[{"role": "user", "content": PROMPT.format(**fmt)}],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("```"))
    return json.loads(text)


def apply_patches(patches: list[dict]) -> list[str]:
    applied = []
    for p in patches:
        path = Path(p["file"])
        if not path.exists():
            raise RuntimeError(f"file not found: {p['file']}")
        content = path.read_text()
        n = content.count(p["find"])
        if n != 1:
            raise RuntimeError(
                f"'find' must appear exactly once in {p['file']} (found {n})"
            )
        path.write_text(content.replace(p["find"], p["replace"], 1))
        applied.append(p["file"])
    return applied


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def open_pr(
    *,
    workflow: str,
    run_id: str,
    run_url: str,
    classification: str,
    root_cause: str,
    commit_msg: str,
    patched: list[str],
) -> str:
    sh(["git", "config", "user.name", "autofix-bot"], capture=False)
    sh(["git", "config", "user.email", "autofix-bot@users.noreply.github.com"], capture=False)

    branch = f"{BRANCH_PREFIX}{slugify(workflow)}-{run_id}"
    sh(["git", "checkout", "-b", branch], capture=False)
    for f in patched:
        sh(["git", "add", f], capture=False)

    full_msg = (
        f"{commit_msg}\n\n"
        f"Autofix for failed run: {run_url}\n\n"
        f"Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
    )
    sh(["git", "commit", "-m", full_msg], capture=False)
    sh(["git", "push", "origin", branch], capture=False)

    files_md = "\n".join(f"- `{f}`" for f in patched)
    body = (
        f"## Auto-fix from failed workflow run\n\n"
        f"**Run:** {run_url}\n"
        f"**Workflow:** {workflow}\n"
        f"**Classification:** ({classification})\n"
        f"**Root cause:** {root_cause}\n\n"
        f"## Changed files\n{files_md}\n\n"
        f"## Test plan\n"
        f"- [ ] Confirm the patch addresses the root cause\n"
        f"- [ ] Re-run the failed workflow manually via workflow_dispatch\n"
        f"- [ ] Confirm next scheduled run succeeds\n\n"
        f"---\n"
        f"Opened automatically by `.github/workflows/autofix-on-failure.yml`.\n"
    )
    title = f"autofix: {workflow} — {commit_msg.removeprefix('fix: ')}"
    out = sh(
        ["gh", "pr", "create", "--title", title, "--body", body, "--base", "main", "--head", branch]
    )
    return out.strip()


def main() -> int:
    required = ["ANTHROPIC_API_KEY", "RUN_ID", "WORKFLOW_NAME", "HEAD_SHA", "RUN_URL"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"Missing env vars: {missing}", file=sys.stderr)
        return 1

    run_id = os.environ["RUN_ID"]
    workflow = os.environ["WORKFLOW_NAME"]
    head_sha = os.environ["HEAD_SHA"]
    run_url = os.environ["RUN_URL"]

    print(f"Autofix starting — workflow={workflow} run_id={run_id}")

    logs = fetch_failed_logs(run_id)
    commits = recent_commits()
    tree = repo_tree()
    claude_md = load_claude_md()

    try:
        decision = ask_claude(
            workflow=workflow,
            run_url=run_url,
            head_sha=head_sha,
            commits=commits,
            tree=tree,
            logs=logs,
            claude_md=claude_md,
        )
    except (json.JSONDecodeError, anthropic.APIError) as e:
        print(f"Claude call failed or returned non-JSON: {e}", file=sys.stderr)
        return 0

    print(f"Decision: {decision.get('decision')}")
    print(f"Classification: ({decision.get('classification')})")
    print(f"Root cause: {decision.get('root_cause')}")

    if decision.get("decision") != "patch":
        print(f"Skipping. Reason: {decision.get('skip_reason', '(none given)')}")
        return 0

    patches = decision.get("patches") or []
    if not patches:
        print("Decision was 'patch' but no patches provided — skipping.")
        return 0

    try:
        patched = apply_patches(patches)
    except RuntimeError as e:
        print(f"Patch apply rejected: {e} — skipping.")
        return 0

    try:
        url = open_pr(
            workflow=workflow,
            run_id=run_id,
            run_url=run_url,
            classification=decision.get("classification", "?"),
            root_cause=decision.get("root_cause", ""),
            commit_msg=decision.get("commit_message", "fix: autofix"),
            patched=patched,
        )
    except subprocess.CalledProcessError as e:
        print(f"PR open failed: {e.stderr or e}", file=sys.stderr)
        return 1

    print(f"PR opened: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
