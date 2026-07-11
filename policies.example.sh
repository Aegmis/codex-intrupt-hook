#!/usr/bin/env bash
#
# Example Aegmis policies for the OpenAI Codex CLI PreToolUse hook.
#
# These route the hook's gated tool calls (Bash / apply_patch) to the right
# Slack approver based on how dangerous the action is. Policies are evaluated in
# ASCENDING priority order — the first match wins — so the most specific / most
# dangerous rules use the LOWEST numbers.
#
# The hook POSTs an approval with:
#   tool_name  = "Bash" | "apply_patch"
#   action     = "bash_command" | "apply_patch"
#   tool_kwargs = { "command": ... }   for BOTH (Bash = shell command;
#                                       apply_patch = the patch text)
# Conditions below match against those tool_kwargs keys.
#
# Conditions use the engine's nested schema:
#   "conditions": { "logic": "AND", "rules": { "<key>": { "<op>": <val> } } }
# Operators: >, <, ==, regex, in.  Keys are matched against tool_kwargs.
#
# NOTE: Codex file edits arrive as tool_name "apply_patch" with the patch in
# tool_kwargs.command — so match file paths via the "command" key with a regex
# against the "*** Update File: <path>" markers inside the patch.
#
# Group approvers dispatch to Slack channel  #approvals-{approver_id}
# e.g. approver_id "sre-team"  ->  #approvals-sre-team
#
# Usage:
#   export AEGMIS_BASE_URL=https://api.aegmis.com
#   export AEGMIS_API_KEY=sk_org_xxxx_yyyy
#   export ORG_ID=org_xxxx
#   ./policies.example.sh

set -euo pipefail

: "${AEGMIS_BASE_URL:?set AEGMIS_BASE_URL}"
: "${AEGMIS_API_KEY:?set AEGMIS_API_KEY}"
: "${ORG_ID:?set ORG_ID}"

create_policy() {
  curl -sS -X POST "$AEGMIS_BASE_URL/org/$ORG_ID/policies" \
    -H "Authorization: Bearer $AEGMIS_API_KEY" \
    -H "Content-Type: application/json" \
    -H "User-Agent: intrupt-hook/1.0" \
    -d "$1"
  echo
}

# ── Priority 5 — hard cases that need a senior approver ──────────────────────
# Recursive/forced deletes, disk wipes: route to SRE.
create_policy '{
  "name": "codex-destructive-shell",
  "description": "Any rm — rm -rf and plain rm <file> — plus dd, mkfs",
  "trigger_tool_names": ["Bash"],
  "conditions": {
    "logic": "AND",
    "rules": {
      "command": { "regex": "\\brm\\s+.*-[a-z]*[rf]|\\brm\\s+|\\bmkfs\\b|\\bdd\\s+if=" }
    }
  },
  "approver_type": "group",
  "approver_id": "sre-team",
  "priority": 5
}'

# ── Priority 10 — production deploys & infrastructure changes ─────────────────
create_policy '{
  "name": "codex-deploy-and-infra",
  "description": "git push, terraform apply/destroy, kubectl apply/delete, deploy",
  "trigger_tool_names": ["Bash"],
  "conditions": {
    "logic": "AND",
    "rules": {
      "command": { "regex": "\\bgit\\s+push\\b|\\bterraform\\s+(apply|destroy)\\b|\\bkubectl\\s+(apply|delete)\\b|\\bdeploy\\b|\\bnpm\\s+publish\\b" }
    }
  },
  "approver_type": "group",
  "approver_id": "platform-team",
  "priority": 10
}'

# ── Priority 15 — patches touching secrets / prod config ─────────────────────
# apply_patch carries the patch text in tool_kwargs.command; match the file
# markers inside it.
create_policy '{
  "name": "codex-protect-secrets",
  "description": "Patches to .env, secrets, or prod config",
  "trigger_tool_names": ["apply_patch"],
  "conditions": {
    "logic": "AND",
    "rules": {
      "command": { "regex": "File:\\s+.*((^|/)\\.env($|\\.)|/secrets?/|/prod/)" }
    }
  },
  "approver_type": "user",
  "approver_id": "U_AMIT_SLACK_ID",
  "priority": 15
}'

# NOTE: With the hook in forward-all mode (AEGMIS_FORWARD_ALL=true), EVERY
# Bash/apply_patch call reaches the policy engine. Do NOT add a catch-all policy
# that matches everything — anything no policy matches is auto-approved, which is
# exactly what keeps routine commands (ls, cat, git status) friction-free. Add
# narrowly-scoped high-risk policies (like those above) and let the rest fall
# through to auto-approve.
