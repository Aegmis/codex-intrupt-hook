#!/usr/bin/env bash
# Installs the intrupt PreToolUse hook into OpenAI Codex CLI.
#
# One-line install (no clone needed):
#   curl -fsSL https://raw.githubusercontent.com/Aegmis/codex-intrupt-hook/main/install.sh | bash
#
# Or, after cloning:
#   bash install.sh

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────

REPO_RAW="${AEGMIS_REPO_RAW:-https://raw.githubusercontent.com/Aegmis/codex-intrupt-hook/main}"

HOOKS_DIR="$HOME/.codex/hooks"
HOOKS_FILE="$HOME/.codex/hooks.json"
HOOK_DEST="$HOOKS_DIR/intrupt_hook.py"
ENV_FILE="$HOME/.codex/.env.intrupt"

# Directory of this script when run from a clone; empty when piped via curl.
if [ -n "${BASH_SOURCE:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  SCRIPT_DIR=""
fi

# ── Helpers ──────────────────────────────────────────────────────────────────

# fetch <relative-path> <dest>
# Uses the local file if this script runs from a clone; otherwise downloads it.
fetch() {
  local rel="$1" dest="$2"
  if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/$rel" ]; then
    cp "$SCRIPT_DIR/$rel" "$dest"
  elif command -v curl &>/dev/null; then
    curl -fsSL "$REPO_RAW/$rel" -o "$dest"
  elif command -v wget &>/dev/null; then
    wget -qO "$dest" "$REPO_RAW/$rel"
  else
    echo "✗ Need curl or wget to download $rel" >&2
    exit 1
  fi
}

# ── Preflight: Codex must be recent enough to support PreToolUse hooks ────────
# Codex shipped its Claude-style lifecycle-hooks engine (incl. PreToolUse, which
# is what lets this gate BLOCK a tool call) in early 2026. Older Codex silently
# ignores the hooks.json config — the gate would be absent with no error. Warn
# loudly rather than give a false sense of protection.
check_codex_version() {
  if ! command -v codex &>/dev/null; then
    echo "⚠  'codex' not found on PATH — cannot verify hook support."
    echo "   Install/updated Codex, then confirm PreToolUse hooks are supported."
    return
  fi
  local ver
  ver="$(codex --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1 || true)"
  echo "→ Detected Codex version: ${ver:-unknown}"
  if [ -z "$ver" ]; then
    echo "⚠  Could not parse the Codex version. PreToolUse hooks require a build"
    echo "   from ~March 2026 or later; on older builds this gate does NOT run."
    return
  fi
  local major minor
  major="${ver%%.*}"
  minor="$(printf '%s' "$ver" | cut -d. -f2)"
  # Hooks first shipped around the 0.114 line; PreToolUse landed shortly after.
  if [ "$major" = "0" ] && [ "${minor:-0}" -lt 114 ]; then
    echo "✗ Codex $ver is too old — PreToolUse hooks are unsupported, so this"
    echo "  gate would be SILENTLY INACTIVE. Upgrade Codex before relying on it."
    echo "  (Set AEGMIS_SKIP_VERSION_CHECK=1 to install anyway.)"
    if [ "${AEGMIS_SKIP_VERSION_CHECK:-0}" != "1" ]; then
      exit 1
    fi
  fi
}

# ── Install hook script ──────────────────────────────────────────────────────

check_codex_version

echo "→ Creating hooks directory: $HOOKS_DIR"
mkdir -p "$HOOKS_DIR"

echo "→ Installing hook script"
fetch "hook.py" "$HOOK_DEST"
chmod +x "$HOOK_DEST"

# ── Merge hooks.json ─────────────────────────────────────────────────────────

HOOKS_JSON='{
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
}'

merge_hooks() {
  if [ ! -f "$HOOKS_FILE" ]; then
    echo "→ Creating $HOOKS_FILE"
    printf '%s\n' "$HOOKS_JSON" > "$HOOKS_FILE"
    return
  fi

  if command -v jq &>/dev/null; then
    echo "→ Merging hooks into existing $HOOKS_FILE"
    tmp=$(mktemp)
    jq -s '.[0] * .[1]' "$HOOKS_FILE" <(printf '%s' "$HOOKS_JSON") > "$tmp"
    mv "$tmp" "$HOOKS_FILE"
    echo "   Merged."
  else
    echo ""
    echo "⚠  jq not found — please manually add the following to $HOOKS_FILE:"
    echo ""
    printf '%s\n' "$HOOKS_JSON"
    echo ""
  fi
}

merge_hooks

# ── Environment variables ────────────────────────────────────────────────────

if [ ! -f "$ENV_FILE" ]; then
  echo "→ Creating env file at $ENV_FILE"
  cat > "$ENV_FILE" <<'EOF'
# intrupt hook configuration — sourced by your shell profile
export AEGMIS_BASE_URL=https://api.aegmis.com
export AEGMIS_API_KEY=sk_org_xxxx_yyyy      # replace with your API key
export AEGMIS_APPROVAL=true            # set false to disable the gate entirely
export AEGMIS_FORWARD_ALL=false        # local mode: the hook decides (no server round-trip)
export AEGMIS_GATED_TOOLS=Bash         # gate shell only (not apply_patch)
export AEGMIS_PROTECTED_PATHS="re:^$HOME$"  # gate rm of the home dir ITSELF (not its contents)
# export AEGMIS_BLOCKED_PATHS="re:^$HOME$"  # HARD-DENY these targets locally (denied instantly, never asks); opt-in
export AEGMIS_TIMEOUT=600
export AEGMIS_POLL_INTERVAL=5
export AEGMIS_CHANNEL=slack           # approval delivery channel: slack | email
EOF
  echo ""
  echo "   Edit $ENV_FILE and fill in your AEGMIS_API_KEY."
  echo "   Then add this to your ~/.zshrc or ~/.bashrc:"
  echo ""
  echo "     source $ENV_FILE"
  echo ""
fi

echo ""
echo "✓ Installation complete."
echo ""
echo "  Hook:  $HOOK_DEST"
echo "  Hooks: $HOOKS_FILE"
echo ""
echo "  Next steps:"
echo "  1. Edit $ENV_FILE with your API key"
echo "  2. Add  source $ENV_FILE  to ~/.zshrc (or ~/.bashrc)"
echo "  3. Restart Codex so it reloads ~/.codex/hooks.json"
echo "  4. TRUST THE HOOK when Codex prompts — Codex will not run a command"
echo "     hook until you review and trust its exact definition. Until then the"
echo "     gate is INACTIVE (fails open)."
echo "  5. Ask Codex to run a gated command (e.g. git push)"
echo ""
echo "  ⚠  RE-TRUST ON UPDATE: Codex records trust against the hook's hash. If"
echo "     you re-run this installer or the hook.py changes, Codex will skip the"
echo "     hook (gate OFF) until you trust it again. Re-confirm trust after every"
echo "     update, and re-check gating with a known-gated command."
echo ""
echo "  NOTE: user-level hooks live in ~/.codex/. Project-local hooks"
echo "        (<repo>/.codex/hooks.json) only load when the project is trusted."
echo ""
