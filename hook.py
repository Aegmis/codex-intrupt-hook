#!/usr/bin/env python3
"""
OpenAI Codex CLI PreToolUse hook — intrupt approval gate.

Reads a tool-call payload from stdin, POSTs to the intrupt API to create a
pending approval (which notifies the approver via Slack), then polls until a
human decides.

Codex hook contract (PreToolUse):
  - stdin  : JSON with tool_name, tool_input, session_id, cwd, ...
             Shell commands arrive with tool_name "Bash" (Codex canonicalizes
             shell_command / exec_command / unified_exec to "Bash" for hooks).
             File edits arrive as tool_name "apply_patch" with the patch text in
             tool_input.command (matcher aliases Write/Edit also map here).
  - BLOCK  : print
               {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                "permissionDecision": "deny", "permissionDecisionReason": "..."}}
             and exit 0. (Exit 0 + this JSON is the safe block form; exit 2 with
             an EMPTY stderr is treated by Codex as a hook *failure*, which does
             NOT block — so we always use the JSON-deny form here.)
  - ALLOW  : print {} and exit 0 (defer to Codex's own approval flow).

This hook is purely ADDITIVE: on approve / non-gated calls it emits {} and
exits 0. On reject / timeout / error / crash it emits a "deny" decision (fail
closed) — a crashed hook would otherwise fail OPEN in Codex.

Environment variables (required):
  AEGMIS_BASE_URL   Base URL of the intrupt approval API (e.g. https://api.aegmis.com)
  AEGMIS_API_KEY    API key from Account → API Keys (org ID is extracted automatically)

Optional:
  AEGMIS_GATED_TOOLS     Comma-separated tool names to gate.
                           Default: Bash,apply_patch
  AEGMIS_FORWARD_ALL     If true (default), forward every gated tool call to the
                           policy engine and let server-side policies decide
                           (unmatched calls are auto-approved). If false, use the
                           local SHELL_GATE_PATTERNS pre-filter for shell commands.
                           A few hard local gates (workspace wipe, self-protection,
                           AEGMIS_BLOCKED_PATHS) always apply, in BOTH modes.
  AEGMIS_TIMEOUT         Max seconds to wait for a decision. Default: 600 (10 min).
                           Keep it below the Codex hook `timeout` (config sets 630).
  AEGMIS_POLL_INTERVAL   Seconds between status polls. Default: 5
  AEGMIS_BYPASS_PATTERNS Comma-separated regex patterns for shell commands that
                           skip approval (allow-list). Matched per command segment.
"""

import json
import os
import re
import shlex
import sys
import time
import uuid
import urllib.request
import urllib.error
from typing import Optional

# ── Configuration ─────────────────────────────────────────────────────────────

BASE_URL       = os.environ.get("AEGMIS_BASE_URL", "https://api.aegmis.com").rstrip("/")
API_KEY        = os.environ.get("AEGMIS_API_KEY", "")
TIMEOUT        = int(os.environ.get("AEGMIS_TIMEOUT", "600"))
POLL_INTERVAL  = int(os.environ.get("AEGMIS_POLL_INTERVAL", "5"))
CHANNEL        = os.environ.get("AEGMIS_CHANNEL", "slack")

FORWARD_ALL = os.environ.get("AEGMIS_FORWARD_ALL", "true").lower() in ("1", "true", "yes")
APPROVAL_ENABLED = os.environ.get("AEGMIS_APPROVAL", "true").lower() not in ("0", "false", "no", "off", "disable", "disabled")

# Codex tool names (hook layer). Shell → "Bash"; file edits → "apply_patch".
SHELL_TOOL = "Bash"
PATCH_TOOL = "apply_patch"

GATED_TOOLS = {
    t.strip()
    for t in os.environ.get("AEGMIS_GATED_TOOLS", "Bash,apply_patch").split(",")
    if t.strip()
}

_HOME = os.path.expanduser("~")

# Shell commands matching ANY of these patterns require approval. Evaluated per
# command SEGMENT (a chain like `a && b | c ; d` is split on && || ; & and
# newlines; pipelines stay intact) so a benign command can't shield a risky one.
SHELL_GATE_PATTERNS: list[str] = [
    r"\brm\b[\s\S]*\s(~/?(\s|$)|\$\{?HOME\}?/?(\s|$)|/(\s|$)|/\*|/(Users|home)/[^/\s]+/?(\s|$)|/(etc|usr|var|bin|sbin|opt|System|Library|private|boot|dev|lib|sys|proc)(/|\s|$)|\*(\s|$)|\.(\s|$)|\.\.(/|\s|$))",
    # ── Destructive / mass deletes beyond plain rm ─────────────────────────────
    r"\bfind\b[\s\S]*\s-delete\b",
    r"\bfind\b[\s\S]*-exec\s+rm\b",
    r"\bgit\s+clean\s+-[a-z]*f",
    r"\brsync\b[\s\S]*--delete\b",
    r"\bshred\b",
    r"\bunlink\b\s",
    # ── History / repo rewrites ────────────────────────────────────────────────
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+(rebase|filter-branch|filter-repo)\b",
    r"\bgit\s+branch\s+-D\b",
    # ── Code / data egress (exfiltration) ──────────────────────────────────────
    r"\bgit\s+push\b",
    r"\bgit\s+remote\s+(add|set-url)\b",
    r"\bgh\s+repo\s+create\b",
    r"\bgh\s+repo\s+edit\b[\s\S]*--visibility",
    r"\bgh\s+gist\s+create\b",
    r"\bgh\s+pr\s+merge\b",
    r"\bgh\s+release\b",
    r"\bcurl\b[\s\S]*(\s-T\b|--upload-file\b|\s-F\b|--form\b|--data-binary\s*@|\s-d\s*@|--data\s*@)",
    r"\bwget\b[\s\S]*--post-file\b",
    r"\bscp\b\s",
    r"\brsync\b[\s\S]*\s[^\s]+@[^\s:]+:",
    r"\b(nc|ncat|netcat)\b\s",
    # ── Publish / release / deploy ─────────────────────────────────────────────
    r"\bnpm\s+publish\b",
    r"\b(pip|twine)\s+upload\b|\btwine\s+upload\b",
    r"\b(cargo\s+publish|gem\s+push|poetry\s+publish)\b",
    r"\bdocker\s+(push|login)\b",
    r"\bdeploy\b",
    r"\bkubectl\s+delete\b",
    r"\bkubectl\s+apply\b",
    r"\bterraform\s+apply\b",
    r"\bterraform\s+destroy\b",
    # ── Database ───────────────────────────────────────────────────────────────
    r"DROP\s+(TABLE|DATABASE|SCHEMA)",
    r"TRUNCATE\s+TABLE",
    # ── Disk / device ──────────────────────────────────────────────────────────
    r"\bdd\s+if=",
    r"\b(mkfs|wipefs|fdisk)\b",
    r">\s*/dev/(sd|nvme|disk|hd)",
    # ── Privilege / perms ──────────────────────────────────────────────────────
    r"\bsudo\b",
    r"\bchmod\s+[0-7]*7[0-7][0-7]\b",
    r"\bchown\b.*root",
    # ── Remote-to-shell & obfuscation ──────────────────────────────────────────
    r"\|\s*(ba|z|k)?sh\b",
    r"\bbase64\b[\s\S]*(-d|-D|--decode)\b",
    r"\beval\b",
    r"\b(ba|z|k)?sh\s+-c\b",
    r"\bxargs\b[\s\S]*\brm\b",
    r"\bpython[0-9.]*\b[\s\S]*-c\b[\s\S]*(rmtree|os\.remove|os\.unlink|shutil)",
    r"\bperl\b[\s\S]*-e\b[\s\S]*unlink",
]

for _pp in os.environ.get("AEGMIS_PROTECTED_PATHS", "").split(","):
    _pp = _pp.strip()
    if _pp and not _pp.startswith("re:"):
        SHELL_GATE_PATTERNS.append(r"\brm\b[\s\S]*\s" + re.escape(_pp.rstrip("/")) + r"(/|\s|$)")

_COMPILED = [re.compile(p, re.IGNORECASE) for p in SHELL_GATE_PATTERNS]

_SEG_SPLIT = re.compile(r"&&|\|\||;|&(?!&)|\n")

_STATE = {"cwd": ""}

_PROTECTED_LITERAL = []
_PROTECTED_REGEX = []
for _pp in os.environ.get("AEGMIS_PROTECTED_PATHS", "").split(","):
    _pp = _pp.strip()
    if not _pp:
        continue
    if _pp.startswith("re:"):
        try:
            _PROTECTED_REGEX.append(re.compile(_pp[3:]))
        except re.error as _exc:
            print(f"[intrupt hook] ignoring invalid AEGMIS_PROTECTED_PATHS regex {_pp[3:]!r}: {_exc}",
                  file=sys.stderr)
    else:
        _PROTECTED_LITERAL.append(os.path.normpath(os.path.expanduser(_pp.rstrip("/"))))

_BLOCKED_LITERAL = []
_BLOCKED_REGEX = []
for _pp in os.environ.get("AEGMIS_BLOCKED_PATHS", "").split(","):
    _pp = _pp.strip()
    if not _pp:
        continue
    if _pp.startswith("re:"):
        try:
            _BLOCKED_REGEX.append(re.compile(_pp[3:]))
        except re.error as _exc:
            print(f"[intrupt hook] ignoring invalid AEGMIS_BLOCKED_PATHS regex {_pp[3:]!r}: {_exc}",
                  file=sys.stderr)
    else:
        _BLOCKED_LITERAL.append(os.path.normpath(os.path.expanduser(_pp.rstrip("/"))))

# Self-protection: writes/deletes/edits touching the hook's own config are always
# gated, regardless of AEGMIS_GATED_TOOLS.
_SELF_PROTECT = [
    os.path.normpath(os.path.join(_HOME, ".codex")),
]
_SELF_PROTECT_SUFFIX = (
    os.path.join(".codex", ""),
    os.path.join(".git", "hooks"),
)
_MUTATING_VERB = re.compile(
    r"\b(rm|mv|cp|tee|truncate|dd|chmod|chown|ln|install|touch)\b|\bsed\s+-i|>\s*\S|>>\s*\S"
)


def _tokenize(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _expand(path: str, cwd: str) -> str:
    p = path
    for var in ("${PWD}", "$PWD"):
        p = p.replace(var, cwd or ".")
    for var in ("${HOME}", "$HOME"):
        p = p.replace(var, _HOME)
    return os.path.expanduser(p)


def _resolve(path: str, cwd: str) -> str:
    p = _expand(path, cwd)
    if not os.path.isabs(p):
        p = os.path.join(cwd or ".", p)
    return os.path.normpath(p).rstrip("/") or "/"


def _path_tokens(command: str) -> list[str]:
    out = []
    for tok in _tokenize(command):
        t = tok.lstrip("<>&|")
        t = t.strip("'\"")
        if not t or t.startswith("-") or t in ("rm", "sudo", "--", "mv", "cp",
                                               "tee", "sed", "ln", "chmod", "chown",
                                               "install", "touch", "cat", "&&", "||", ";", "|"):
            continue
        out.append(t)
    return out


def _rm_hits(command: str, literals: list, regexes: list) -> bool:
    if (not literals and not regexes) or not re.search(r"\brm\b", command):
        return False
    for t in _path_tokens(command):
        cand = _resolve(t, _STATE["cwd"])
        for prot in literals:
            if cand == prot or cand.startswith(prot + "/"):
                return True
        for _rx in regexes:
            if _rx.search(cand):
                return True
    return False


def _rm_hits_protected(command: str) -> bool:
    return _rm_hits(command, _PROTECTED_LITERAL, _PROTECTED_REGEX)


def _rm_hits_blocked(command: str) -> bool:
    return _rm_hits(command, _BLOCKED_LITERAL, _BLOCKED_REGEX)


# Write/create gate for AEGMIS_PROTECTED_PATHS — gate not just `rm` of a protected
# path but also file CREATION / writes INTO it (touch, tee, cp/mv, `>`/`>>`
# redirection). Scoped to protected dirs only, so writes elsewhere stay free.
# Mirrors the rm-based protected-path gate.
_WRITE_VERB = re.compile(r"\b(touch|tee|cp|mv|install|dd|ln)\b|>\s*\S|>>\s*\S")


def _write_hits(command: str, literals: list, regexes: list) -> bool:
    """True if a write/create verb targets a path under a literal dir (+subtree)
    or a `re:` regex."""
    if (not literals and not regexes) or not _WRITE_VERB.search(command):
        return False
    for t in _path_tokens(command):
        cand = _resolve(t, _STATE["cwd"])
        for prot in literals:
            if cand == prot or cand.startswith(prot + "/"):
                return True
        for _rx in regexes:
            if _rx.search(cand):
                return True
    return False


def _write_hits_protected(command: str) -> bool:
    return _write_hits(command, _PROTECTED_LITERAL, _PROTECTED_REGEX)


def _rm_hits_workspace(command: str) -> bool:
    """True if a delete targets the whole project — the working dir itself or any
    ancestor (or filesystem root). Deleting a SUBDIR stays free."""
    cwd = _STATE["cwd"]
    if not cwd:
        return False
    if not re.search(r"\b(rm|find)\b", command):
        return False
    cwd_n = os.path.normpath(cwd).rstrip("/") or "/"
    for t in _path_tokens(command):
        cand = _resolve(t, cwd)
        if cand == "/" or cand == cwd_n or cwd_n.startswith(cand + "/"):
            return True
    return False


def _path_under_self_protect(cand: str) -> bool:
    for prot in _SELF_PROTECT:
        if cand == prot or cand.startswith(prot + "/"):
            return True
    norm = cand.replace("\\", "/")
    for suffix in _SELF_PROTECT_SUFFIX:
        s = suffix.replace("\\", "/").rstrip("/")
        if norm == s or ("/" + s + "/") in (norm + "/") or norm.endswith("/" + s):
            return True
    return False


def _hits_self_protect(command: str) -> bool:
    if not _MUTATING_VERB.search(command):
        return False
    for t in _path_tokens(command):
        if _path_under_self_protect(_resolve(t, _STATE["cwd"])):
            return True
    return False


_BYPASS_RAW = os.environ.get("AEGMIS_BYPASS_PATTERNS", "")
_BYPASS = [re.compile(p, re.IGNORECASE) for p in _BYPASS_RAW.split(",") if p.strip()]

# apply_patch file markers: "*** Add File: path", "*** Update File: path", ...
_PATCH_FILE_RE = re.compile(r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s+(.+)$", re.MULTILINE)


def _segments(command: str) -> list[str]:
    segs = [s.strip() for s in _SEG_SPLIT.split(command) if s.strip()]
    return segs or [command]


def _segment_bypassed(seg: str) -> bool:
    return any(b.search(seg) for b in _BYPASS)


def _fully_bypassed(command: str) -> bool:
    if not _BYPASS:
        return False
    return all(_segment_bypassed(s) for s in _segments(command))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_org_id(api_key: str) -> str:
    if not api_key.startswith("sk_org_"):
        _die("Invalid AEGMIS_API_KEY format — expected 'sk_org_{org_id}_{hash}'")
    after_prefix = api_key[7:]
    last_underscore = after_prefix.rfind("_")
    if last_underscore == -1:
        _die("Invalid AEGMIS_API_KEY format — expected 'sk_org_{org_id}_{hash}'")
    org_id = after_prefix[:last_underscore]
    if not org_id.startswith("org_"):
        _die(f"Could not extract org ID from API key — got '{org_id}'")
    return org_id


def _api(method: str, path: str, body: Optional[dict] = None) -> dict:
    url  = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent":    "intrupt-hook/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        _die(f"intrupt API {method} {path} → HTTP {exc.code}: {body_text}")
    except urllib.error.URLError as exc:
        _die(f"intrupt API unreachable ({exc.reason}). Is AEGMIS_BASE_URL correct?")


def _allow() -> None:
    """Defer to Codex's normal flow — emit empty JSON, exit 0."""
    print("{}", flush=True)
    sys.exit(0)


def _block(reason: str) -> None:
    """Deny the tool call. Exit 0 + JSON-deny is the safe block form in Codex
    (exit 2 with empty stderr is treated as a hook FAILURE and does not block)."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName":            "PreToolUse",
            "permissionDecision":       "deny",
            "permissionDecisionReason": reason,
        }
    }), flush=True)
    sys.exit(0)


def _die(msg: str) -> None:
    _block(f"[intrupt hook error] {msg}")


def _hard_local_gate(command: str) -> bool:
    """Local gates that ALWAYS apply (both modes). Returns True if approval is
    required; may _block() directly for an outright deny."""
    if _rm_hits_blocked(command):
        _block("Deletion of a hard-blocked path is denied "
               "(AEGMIS_BLOCKED_PATHS) — not sent for approval.")
    if _rm_hits_workspace(command):
        return True
    if _hits_self_protect(command):
        return True
    return False


def _should_gate_shell(command: str) -> bool:
    """Local-mode risk decision, evaluated per command segment."""
    for seg in _segments(command):
        if _segment_bypassed(seg):
            continue
        if _rm_hits_protected(seg):
            return True
        if _write_hits_protected(seg):
            return True
        if any(p.search(seg) for p in _COMPILED):
            return True
    return False


def _patch_files(patch: str) -> list[str]:
    return _PATCH_FILE_RE.findall(patch or "")


# ── Main ────────────────────────────────────────────────────────────────────────

def main() -> None:
    raw = sys.stdin.read()
    if not APPROVAL_ENABLED:
        _allow()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _die("Could not parse hook payload from stdin")

    _STATE["cwd"] = payload.get("cwd") or payload.get("working_dir") or ""

    tool_name  = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            tool_input = {"command": tool_input}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}

    if tool_name not in GATED_TOOLS:
        _allow()

    if tool_name == SHELL_TOOL:
        command = tool_input.get("command", "")
        # Hard local gates apply in BOTH modes (deny / always-ask).
        if not _hard_local_gate(command):
            if FORWARD_ALL:
                if _fully_bypassed(command):
                    _allow()
            else:
                if not _should_gate_shell(command):
                    _allow()
        action  = "bash_command"
        message = f"Run: `{command.splitlines()[0][:120] if command else ''}`"

    elif tool_name == PATCH_TOOL:
        # File edits — always gated (both modes forward to the policy engine).
        files = _patch_files(tool_input.get("command", ""))
        target = ", ".join(f"`{f}`" for f in files) if files else "files"
        action  = "apply_patch"
        message = f"Apply patch to: {target}"

    else:
        action  = tool_name.lower()
        message = f"Codex wants to call `{tool_name}`"

    if not API_KEY:
        _die("AEGMIS_API_KEY is not set")
    org_id = _extract_org_id(API_KEY)

    thread_id = str(uuid.uuid4())

    resp = _api("POST", f"/org/{org_id}/approval", {
        "thread_id":   thread_id,
        "action":      action,
        "message":     message,
        "channel":     CHANNEL,
        "tool_name":   tool_name,
        "tool_kwargs": tool_input,
        "adapter":     "codex",
    })

    status = resp.get("status", "pending")
    if status == "approved":
        _allow()
    if status in ("rejected", "denied"):
        _block(f"Approval rejected (status={status})")

    approval_id = resp.get("approval_id") or resp.get("audit_id")
    if not approval_id:
        _die(f"API did not return approval_id/audit_id: {resp}")

    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        status_resp = _api("GET", f"/org/{org_id}/approval/{approval_id}")
        status = status_resp.get("status", "pending")
        if status == "approved":
            _allow()
        if status in ("rejected", "denied"):
            _block(f"Approval rejected by approver (approval_id={approval_id})")

    _block(
        f"Approval timed out after {TIMEOUT}s — tool call blocked "
        f"(approval_id={approval_id}). Approve or reject it in the dashboard."
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — fail closed on ANY crash
        _block(f"[intrupt hook error] unexpected failure: {exc!r}")
