# codex-intrupt-hook

An OpenAI Codex CLI `PreToolUse` hook that gates high-risk tool calls behind a human approval. Before Codex executes a destructive shell command or applies a file patch, it pauses, notifies your approver via Slack (or any intrupt channel), and waits. The tool only runs if a human clicks **Approve**.

```
Codex CLI
  └─ wants to run: git push origin main
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

## Prerequisites

- Codex CLI with hooks support (`~/.codex/hooks.json` or `[hooks]` in `config.toml`)
- Python 3.10+
- An [Aegmis](https://aegmis.com) account with an API key
- Slack workspace connected to your Aegmis org (for the default channel)

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
  "tool_input": { "command": "git push origin main" }
}
```

Shell commands arrive as `tool_name: "Bash"`; file edits arrive as `tool_name: "apply_patch"` with the patch text in `tool_input.command`.

### 2. The hook decides whether to gate

Not every shell command is dangerous. In local mode the hook checks the command against a list of patterns; low-risk commands (`ls`, `git status`, `cat`, `grep`, etc.) pass through immediately.

Commands that **always require approval**:

| Pattern | Example |
|---|---|
| `rm -rf` / `rm -r` | `rm -rf dist/` |
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
  "message":     "Run: `git push origin main`",
  "channel":     "slack",
  "tool_name":   "Bash",
  "tool_kwargs": { "command": "git push origin main" }
}
```

Your Slack channel receives an interactive message:

```
Codex wants to run:
  git push origin main

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
| `AEGMIS_BYPASS_PATTERNS` | no | — | Comma-separated regex patterns; matching shell commands skip approval |

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

## Testing

Run the included smoke tests — no real API credentials needed:

```bash
python3 test_hook.py
```

Expected output:

```
[PASS] Bash — git push (gated)
[PASS] Bash — ls (allowed)
[PASS] Bash — rm -rf (gated)
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
echo '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}' \
  | python3 hook.py
```

You should see a Slack message appear within a few seconds.

---

## Security notes

- The hook **fails closed**: if the API is unreachable, the env vars are missing, the request times out, or the hook itself crashes, the tool call is denied — not allowed.
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
