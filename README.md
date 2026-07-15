# codex-intrupt-hook

An OpenAI Codex CLI `PreToolUse` hook that gates high-risk tool calls behind a human approval. Before Codex executes a destructive shell command or applies a file patch, it pauses, notifies your approver via Slack (or any intrupt channel), and waits. The tool only runs if a human clicks **Approve**.

```
Codex CLI
  │
  ├─ rm -rf /home/user          (matches AEGMIS_BLOCKED_PATHS)
  │     ⇒  ⛔ denied locally — no API call, no Slack
  │
  └─ kubectl delete pod nginx   (matches a risk pattern)
        │
        ▼
  PreToolUse hook fires
        │
        ▼
  POST /org/{id}/approval  ──►  intrupt API  ──►  Slack message
        │                                              │
        │  poll every 5s                     human clicks Approve / Reject
        │                                              │
        ▼                                              ▼
  GET /approval/{id}  ◄──────────────────────  status = "approved"
        │
        ▼
  {}                                          →  Codex continues
  {"hookSpecificOutput":{…"deny"…}}           →  Codex is blocked
```

---

## Quick start

```bash
# 1. Install
curl -fsSL https://raw.githubusercontent.com/Aegmis/codex-intrupt-hook/main/install.sh | bash

# 2. Set your API key, then load the env
nano ~/.codex/.env.intrupt          # set AEGMIS_API_KEY=sk_org_...
source ~/.codex/.env.intrupt        # also add this line to ~/.zshrc or ~/.bashrc

# 3. Restart Codex — done. High-risk actions now pause for Slack approval.
```

Installer defaults: **local mode**, **shell-only** gating, and deleting the home
dir itself routes to approval (`AEGMIS_PROTECTED_PATHS=re:^$HOME$`). To make a path
**impossible to delete** — denied instantly, never sent to a human — add it to
`AEGMIS_BLOCKED_PATHS` (e.g. `export AEGMIS_BLOCKED_PATHS=re:^$HOME$` in your env file).

---

## Prerequisites

- **Codex CLI new enough to support `PreToolUse` hooks.** Codex shipped its
  Claude-style lifecycle-hooks engine in early 2026 (~the 0.114 line); older
  builds **silently ignore** `hooks.json`, so the gate would be absent with no
  error. `install.sh` checks your version and refuses on builds it knows are too
  old (override with `AEGMIS_SKIP_VERSION_CHECK=1`).
- Python 3.10+
- An [Aegmis](https://aegmis.com) account with an API key
- Slack workspace connected to your Aegmis org (for the default channel)

> **⚠ You must _trust_ the hook, and re-trust it after every update.** Codex will
> not run a command hook until you review and trust its exact definition, and it
> records that trust against the hook's **hash**. If `hook.py` or the command
> changes (e.g. you re-run the installer), Codex **skips the hook** — the gate is
> off — until you trust it again. After any update, re-confirm trust and re-test
> with a known-gated command (e.g. `git push`).

---

## Installation

Install with a single command — no clone required:

```bash
curl -fsSL https://raw.githubusercontent.com/Aegmis/codex-intrupt-hook/main/install.sh | bash
```

<details>
<summary>Prefer to clone first?</summary>

```bash
git clone https://github.com/Aegmis/codex-intrupt-hook.git
cd codex-intrupt-hook
bash install.sh
```

</details>

`install.sh` does three things:

1. Copies `hook.py` to `~/.codex/hooks/intrupt_hook.py`
2. Merges the hook trigger into `~/.codex/hooks.json`
3. Creates `~/.codex/.env.intrupt` with placeholder env vars

Then fill in your credentials and **restart Codex** so it reloads the hooks:

```bash
# Edit the generated env file
nano ~/.codex/.env.intrupt

# Source it (add this line to ~/.zshrc or ~/.bashrc too)
source ~/.codex/.env.intrupt
```

`.env.intrupt`:
```bash
export AEGMIS_BASE_URL=https://api.aegmis.com
export AEGMIS_API_KEY=sk_org_xxxx_yyyy    # Account → API Keys
```

---

## How it works

### 1. Codex fires the hook

Whenever Codex attempts a `Bash` (shell) or `apply_patch` (file edit) tool call, it passes a JSON payload to `hook.py` on stdin before executing anything (the `PreToolUse` event):

```json
{
  "session_id": "…",
  "cwd": "/home/you/project",
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": { "command": "rm -rf /home/user" }
}
```

Shell commands arrive as `tool_name: "Bash"`; file edits arrive as `tool_name: "apply_patch"` with the patch text in `tool_input.command`.

### 2. The hook decides whether to gate

Not every shell command is dangerous. In local mode the hook checks the command against a list of patterns; low-risk commands (`ls`, `git status`, `cat`, `grep`, etc.) pass through immediately.

Commands that **always require approval**:

| Pattern | Example |
|---|---|
| **catastrophic delete** (home / root / system dir, or bare `*` `.` `..`) | `rm -rf ~`, `rm -rf /`, `rm -rf /Users/you`, `rm *` — **not** `rm file` or `rm -rf node_modules` |
| `git push` | `git push origin main --force` |
| `git reset --hard` | `git reset --hard HEAD~3` |
| `gh pr merge` / `gh release` | `gh pr merge 42` |
| `npm publish` | `npm publish --access public` |
| `deploy` | `npm run deploy`, `./deploy.sh` |
| `kubectl delete` / `kubectl apply` | `kubectl delete pod my-pod` |
| `terraform apply` / `terraform destroy` | `terraform destroy -auto-approve` |
| `DROP TABLE` / `TRUNCATE TABLE` | SQL run via CLI |
| `sudo` | `sudo systemctl restart nginx` |
| `curl ... \| sh` | piped install scripts |
| `dd if=` / `mkfs` | disk operations |

`apply_patch` (any file edit) always requires approval. In **forward-all mode** (the default), every gated call is instead sent to the Aegmis policy engine, which decides based on your server-side policies (unmatched calls auto-approve).

### 3. Approval is requested

The hook calls the intrupt API to create a pending approval:

```
POST /org/{org_id}/approval
{
  "thread_id":   "<uuid>",
  "action":      "bash_command",
  "message":     "Run: `rm -rf /home/user`",
  "channel":     "slack",
  "tool_name":   "Bash",
  "tool_kwargs": { "command": "rm -rf /home/user" }
}
```

Your Slack channel receives an interactive message:

```
Codex wants to run:
  rm -rf /home/user

[ ✅ Approve ]  [ ❌ Reject ]
```

### 4. The hook polls for a decision

The hook polls `GET /org/{org_id}/approval/{approval_id}` every 5 seconds until:

| Outcome | Hook stdout | Codex |
|---|---|---|
| Human clicks **Approve** | `{}` | Tool runs normally |
| Human clicks **Reject** | `{"hookSpecificOutput":{…"deny"…}}` | Tool blocked, reason shown to Codex |
| Timeout (default 10 min) | `{"hookSpecificOutput":{…"deny"…}}` | Tool blocked with timeout message |
| API unreachable | `{"hookSpecificOutput":{…"deny"…}}` | Tool blocked (fail closed) |

The hook always exits `0` and signals its decision via `permissionDecision`. On
approve or a non-gated call it emits an empty object `{}`, deferring to Codex's
own approval flow. **This hook only ever adds a gate.**

> **Fail-closed on crash:** Codex treats a crashed hook (non-zero exit) as
> fail-*open* — the tool would proceed. To prevent that, `hook.py` wraps its
> whole run and converts any unexpected exception into an explicit `deny`.

---

## What gets gated

Two tiers, evaluated in **local mode** (`AEGMIS_FORWARD_ALL=false`, the installer default):

**Hard-blocked — denied instantly, never sent to a human** (`AEGMIS_BLOCKED_PATHS`)

Only an `rm` whose target (resolved against the command's cwd, so relative paths
count) matches a `AEGMIS_BLOCKED_PATHS` entry. Denied locally with no approval
round-trip. Opt-in — nothing is hard-blocked unless you list it.

**Gated — paused for Slack approval**

The hook ships **20 built-in risk patterns**, identical across all 9 hooks. Several are families (one pattern, many commands), so they cover **30+ distinct dangerous commands**:

| Category | Matches | Passes through |
|---|---|---|
| Catastrophic `rm` | `rm -rf ~`, `rm -rf /`, `rm -rf /Users/you`, `rm *`, `rm -rf .` | `rm file.txt`, `rm -rf node_modules`, `rm -rf build` |
| Protected paths | `rm` of any dir in `AEGMIS_PROTECTED_PATHS` (default `re:^$HOME$`) + its subtree | anything not listed |
| Git | `git push` (incl. `--force`), `git reset --hard` | `git status`, `git commit`, `git pull` |
| Publish / release | `gh pr merge`, `gh release`, `npm publish`, `deploy` | builds, tests |
| Infra | `kubectl delete`/`apply`, `terraform apply`/`destroy` | `kubectl get`, `terraform plan` |
| Database | `DROP TABLE`, `TRUNCATE TABLE` | `SELECT`, `INSERT` |
| Disk | `dd if=`, `mkfs` | — |
| Privilege / perms | `sudo`, `chmod 777`, `chown … root` | `chmod 644` |
| Remote-to-shell | `curl … \| sh`, `wget -O- … \| sh` | plain `curl`/`wget` downloads |

Plus any **file write/edit** tool call is gated whenever that tool is in
`AEGMIS_GATED_TOOLS` — the installer default gates the **shell only**, so file
writes run free out of the box until you add them.

Everything else — reads, listings, `ls`, routine deletes — runs untouched. In
**forward-all mode** (`AEGMIS_FORWARD_ALL=true`) these local patterns are bypassed
and every gated tool call is sent to the **server-side policy engine** instead,
where your Aegmis policies decide — any command you write a policy for. The
`policies.example.sh` reference ships **~23 more** ready-to-use destructive-action
regexes (`find -delete`, `shred`, `docker push`, `crontab -r`, cloud-CLI deletes,
`kill`/`shutdown`, and more).

---

## Guarding your paths (approval vs hard-block)

Two env vars control what happens when the agent tries to `rm` a path you care
about. Both take a comma-separated list of **literal dirs** or **`re:`-prefixed
regexes**, resolved against the command's cwd (so relative targets like `./work`
are caught too).

| Variable | A matching `rm`… | Reach for it when |
|---|---|---|
| `AEGMIS_PROTECTED_PATHS` | pauses for **Slack approval** — a human can still allow it | the path matters but is *sometimes* legitimately deleted |
| `AEGMIS_BLOCKED_PATHS` | is **denied locally, instantly** — no Slack, nothing to approve | the path must **never** be deleted by the agent |

If a path matches **both**, the hard block wins — it's checked first, before any
approval round-trip. Both are **local-mode** features (`AEGMIS_FORWARD_ALL=false`,
the installer default).

### Minimal steps

1. Open your env file: `~/.codex/.env.intrupt`
2. Add either variable — one path or many, comma-separated:

   ```bash
   # Ask a human before deleting these  →  approval
   export AEGMIS_PROTECTED_PATHS="$HOME/work,$HOME/important"

   # Never let the agent delete these   →  hard block (no approval)
   export AEGMIS_BLOCKED_PATHS="re:^$HOME$,$HOME/.ssh"
   ```
3. Reload it: `source ~/.codex/.env.intrupt` (or restart Codex).

### Examples

| Goal | Entry |
|---|---|
| Approve before wiping the home dir itself | `AEGMIS_PROTECTED_PATHS=re:^$HOME$` |
| Approve deletes of `work` + `important` (and their subtrees) | `AEGMIS_PROTECTED_PATHS=re:^$HOME/(work\|important)(/\|$)` |
| Hard-block `~/.ssh` and everything under it | `AEGMIS_BLOCKED_PATHS=$HOME/.ssh` |
| Hard-block the home dir itself (its contents still run free) | `AEGMIS_BLOCKED_PATHS=re:^$HOME$` |
| Mix — approve `work`, hard-block `~/.ssh` | `AEGMIS_PROTECTED_PATHS=$HOME/work` · `AEGMIS_BLOCKED_PATHS=$HOME/.ssh` |

---

## Configuration

All configuration is via environment variables.

| Variable | Required | Default | Description |
|---|---|---|---|
| `AEGMIS_BASE_URL` | yes | — | intrupt API base URL |
| `AEGMIS_API_KEY` | yes | — | API key from Account → API Keys |
| `AEGMIS_APPROVAL` | no | `true` | Master kill switch — set `false` to disable the gate entirely (allow all) |
| `AEGMIS_GATED_TOOLS` | no | `Bash,apply_patch` | Comma-separated tool names to gate |
| `AEGMIS_FORWARD_ALL` | no | `true` | Forward every gated call to the policy engine (unmatched auto-approve). If `false`, use the local shell pattern pre-filter |
| `AEGMIS_TIMEOUT` | no | `600` | Max seconds to wait for a decision |
| `AEGMIS_POLL_INTERVAL` | no | `5` | Seconds between status polls |
| `AEGMIS_CHANNEL` | no | `slack` | Where the approval request is delivered — `slack` or `email` |
| `AEGMIS_BYPASS_PATTERNS` | no | — | Comma-separated regex patterns; matching shell commands skip approval |
| `AEGMIS_PROTECTED_PATHS` | no | `re:^$HOME$` (set by installer) | Comma-separated dir(s) to also gate `rm` on — each dir **and everything under it**, cwd-resolved. List **one or many** (e.g. `~/work,~/secrets`). Prefix an entry with **`re:`** for a regex tested against the resolved absolute path, e.g. `re:^$HOME$` (home dir only) or `re:^$HOME/(work\|important)(/\|$)` |
| `AEGMIS_BLOCKED_PATHS` | no | — | Same syntax as `AEGMIS_PROTECTED_PATHS`, but an `rm` hitting one is **denied locally with no approval round-trip** — never sent to a human. Use for paths that must *never* be deleted. **Local mode only** (`AEGMIS_FORWARD_ALL=false`). |

**Approval channel:** requests go to **Slack** by default. To deliver them over **email** instead, set `AEGMIS_CHANNEL=email` in your env file.

> **Hook timeout:** Codex hook `timeout` is in **seconds**. The bundled config
> sets `630` so it exceeds `AEGMIS_TIMEOUT` (600 s). If you raise
> `AEGMIS_TIMEOUT`, raise the hook `timeout` to match.

### Allow-listing specific commands

```bash
# Allow git push to a specific remote only
export AEGMIS_BYPASS_PATTERNS="git push staging"

# Allow terraform apply only in a non-prod directory
export AEGMIS_BYPASS_PATTERNS="terraform apply.*-var-file=dev\.tfvars"
```

Bypass patterns are checked first — they take precedence over gate patterns.

---

## Codex settings

`install.sh` writes the following to `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|apply_patch|Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.codex/hooks/intrupt_hook.py",
            "timeout": 630,
            "statusMessage": "Awaiting intrupt approval"
          }
        ]
      }
    ]
  }
}
```

Prefer inline TOML? Paste `config.snippet.toml` into `~/.codex/config.toml` instead (use one or the other, not both). The `matcher` is a regex compared against `tool_name`.

> **Note:** user-level hooks live in `~/.codex/`. Project-local hooks
> (`<repo>/.codex/hooks.json`) only load when the project layer is trusted.

---

## Example: catastrophic-deletion gate + protecting your own paths

In **local mode** (`AEGMIS_FORWARD_ALL=false`) the hook gates only *catastrophic*
deletions and lets routine ones run untouched:

```bash
rm abc.txt                 # runs   — routine single-file delete
rm -rf node_modules        # runs   — project-local
rm -rf ~                   # ⛔ approval — wipes home
rm -rf /                   # ⛔ approval — wipes root
rm *                       # ⛔ approval — bare glob
```

To also require approval before deleting **specific dirs of yours**, list them:

```bash
export AEGMIS_PROTECTED_PATHS=/Users/you/work,/Users/you/important
```

### `AEGMIS_PROTECTED_PATHS` — literal paths and `re:` regexes

Comma-separated entries — each a **literal** dir or a **`re:`**-prefixed **regex** (the regex is tested against the resolved absolute `rm` target):

| Entry | Effect |
|---|---|
| `re:^$HOME$` | gate `rm` of the **home dir itself only** — `rm -rf ~` gates, but `rm -rf ~/project` and `rm ~/notes.txt` run free *(installer default)* |
| `re:^$HOME/(work\|important)(/\|$)` | gate the `work` + `important` **subtrees** |
| `~/work,re:^$HOME$` | **mixed** — literal `work` subtree *and* regex home-exact both gate; anything else runs free |
| `~/work` | plain **literal** — that dir and everything under it |

Anchor a regex with `^…$` to match a dir exactly (not its contents). Invalid regexes are skipped with a stderr warning.

**Worked examples** (write these as `AEGMIS_PROTECTED_PATHS` entries; `$HOME` expands when the env file is sourced):

| Intent | Entry |
|---|---|
| Protect **only the home dir itself**, not its contents | `re:^$HOME$` |
| Protect `work` + `important` (and their subtrees) | `re:^$HOME/(work\|important)(/\|$)` |
| Protect `project/demo` **except** `project/demo/scratch` | `re:^$HOME/project/demo/(?!scratch(/\|$)).*` |
| Protect any `.env` / secrets file anywhere under home | `re:^$HOME/.*(\.env(\|\.)\|/secrets?/)` |
| Multiple, mixed with literal | `$HOME/work,re:^$HOME$` |


Targets are resolved against the command's working directory, so relative refs are
caught too:

```bash
# with AEGMIS_PROTECTED_PATHS=/Users/you/work
cd /Users/you && rm -rf ./work     # ⛔ approval  (./work → /Users/you/work)
rm -rf /Users/you/work/build       # ⛔ approval  (under a protected dir)
rm -rf /Users/you/other            # runs        — not protected
```

---

## Testing

Run the included smoke tests — no real API credentials needed:

```bash
python3 test_hook.py
```

Expected output:

```
[PASS] Bash — git push (gated)
[PASS] Bash — ls (allowed)
[PASS] Bash — rm -rf ~ (catastrophic, gated)
[PASS] Bash — git status (allowed)
[PASS] apply_patch — file edit (gated)
[PASS] read (unknown tool) — not gated
[PASS] Bash — deploy (gated)
[PASS] Bash — sudo apt (gated)
[PASS] Bash — curl | sh (gated)

Results: 9/9 passed ✓
```

To test with a real approval request, set your credentials and run:

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf /home/user"}}' \
  | python3 hook.py
```

You should see a Slack message appear within a few seconds.

---

## Defense in depth — pair the hook with Codex's sandbox & rules

This hook gates the agent's **declared** tool calls by matching a command string,
so a determined agent can evade a pattern denylist. Codex already ships stronger,
OS-level controls — run the gate alongside them:

- **Sandbox:** keep the default `workspace-write` mode. Writes are confined to the
  workspace, `.git`/`.codex` stay read-only, and **network is off by default** —
  which shuts down the "push the codebase somewhere public" class outright. Only
  raise to `danger-full-access` inside an already-isolated container.
- **execpolicy `.rules`:** for actions that must **never** run (no human should
  even be asked), add a `forbidden` rule in `~/.codex/rules/default.rules`. Codex
  splits `&&`/`||`/`;`/`|` chains and applies most-restrictive-wins, so this is a
  robust hard-deny for `rm` of the project/home and for exfil verbs.

Think of it as: **sandbox = the wall, `forbidden` rules = tripwires that never
ask, this hook = the doorbell for the ambiguous middle.**

## Security notes

- The hook **fails closed**: on reject, timeout, unreachable API, missing config,
  or a crash, the tool call is **denied** (exit 0 + a `permissionDecision:"deny"`
  decision — the block form Codex honors; an exit-2-with-empty-stderr would be
  read as a hook *failure* and would **not** block).
- **Workspace & self-protection always apply** (both modes): wiping the project
  dir or an ancestor (`rm -rf .`, `rm -rf "$HOME"`, `find . -delete`, `git clean -fdx`)
  is gated, and shell edits to the hook's own config (`~/.codex/…`) are gated.
- Command **chains are split** (`&&`, `||`, `;`, `|`) and judged per segment, so a
  benign first command can't shield a risky one.
- `AEGMIS_API_KEY` is sent as a `Bearer` token. Keep it out of your shell history and `.bashrc` — use a secrets manager or the `.env.intrupt` file with `600` permissions.
- The hook never stores or logs the tool input beyond what is sent to the API.

---

## Project structure

```
codex-intrupt-hook/
├── hook.py              # PreToolUse hook script (zero runtime dependencies)
├── test_hook.py         # Smoke tests for gating logic
├── install.sh           # One-line installer (curl-pipe or from a clone)
├── hooks.json           # Codex hooks config snippet (JSON)
├── config.snippet.toml  # Inline TOML alternative for config.toml
├── policies.example.sh  # Example Aegmis approval policies
├── .env.example         # Environment variable template
└── README.md
```

---

## Uninstalling

```bash
rm ~/.codex/hooks/intrupt_hook.py
```

Then remove the `PreToolUse` block from `~/.codex/hooks.json` (or the `[hooks]` block from `config.toml`) and restart Codex.
