# Changelog

All notable changes to `codex-intrupt-hook` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com); dates are ISO-8601.

## [0.0.5] - 2026-07-15

### Added
- **Installer version/feature check** — `install.sh` detects the Codex version and
  refuses (overridable with `AEGMIS_SKIP_VERSION_CHECK=1`) on builds too old to support
  `PreToolUse` hooks, where the gate would otherwise be silently inactive.
- **Whole-project delete gate** — an `rm` / `find` whose resolved target is the working
  directory, an ancestor of it, or `/` is gated (`rm -rf .`, `rm -rf "$HOME"`, `$PWD`,
  `..`). Deleting a subdir (`rm -rf build`) still runs free.
- **Protected-path WRITE gate** — a write/create verb (`touch`, `tee`, `cp`, `mv`,
  `install`, `dd`, `ln`, or `>` / `>>`) targeting a path under `AEGMIS_PROTECTED_PATHS`
  is gated. Opt-in and directory-scoped; writes elsewhere and all reads run free.
- **Self-protection** — mutating shell commands touching the hook's own config
  (`~/.codex/…`, `.git/hooks`) are always gated, regardless of `AEGMIS_GATED_TOOLS`.
- Broader denylist: exfiltration (`gh repo/gist create`, `git remote add/set-url`,
  `curl --data-binary/-T/-F`, `scp`, `rsync host:`, `nc`), mass-delete (`find -delete`,
  `git clean -f`, `rsync --delete`, `shred`), and obfuscation shapes (pipe-to-shell,
  `base64 -d`, `eval`, `sh -c`, `xargs rm`).

### Changed
- **Command chains are split** on `&&`, `||`, `;`, `|` and each segment is evaluated
  independently; bypass patterns match per-segment, not anywhere in the whole command.
- **Shell-aware parsing** — targets tokenized with `shlex` and expanded (`~`, `$HOME`,
  `$PWD`), closing evasions like quoted `rm -rf "$HOME"` and `rm -rf ./`.
- `AEGMIS_BLOCKED_PATHS` and the workspace / self-protect gates apply in **both**
  forward-all and local mode.
- README documents the trust model: Codex trusts a command hook by **hash**, so the hook
  must be re-trusted after any update or it is skipped (fails open) until then.

### Notes
- The block contract was already fail-closed (exit 0 + `permissionDecision:"deny"`, with
  a crash guard); no change was needed there.

## [0.0.4] - 2026-07-12

### Added
- `AEGMIS_BLOCKED_PATHS` — **hard local deny** for `rm`: a matching deletion is blocked
  instantly with no approval round-trip (never sent to a human), a stronger sibling of
  `AEGMIS_PROTECTED_PATHS`. Same syntax (literal dir + subtree, or `re:` regex tested
  against the resolved absolute target); **local mode only**, and checked *before* the
  approval gate, so a hard block wins if a path is in both lists.
- `AEGMIS_CHANNEL` — approval delivery channel: `slack` (default) or `email`.

### Changed
- Installer ships a commented `AEGMIS_BLOCKED_PATHS` opt-in line and sets
  `AEGMIS_CHANNEL=slack` in the env template.
- README substantially expanded: a **Quick start**, a **What gets gated** two-tier
  reference (hard-block vs approval, plus the 20 built-in risk patterns), a two-branch
  flow diagram (local deny vs Slack approval), and a **Guarding your paths** section
  with minimal steps and `AEGMIS_PROTECTED_PATHS` / `AEGMIS_BLOCKED_PATHS` examples.

## [0.0.2] - 2026-07-11

### Added
- `AEGMIS_PROTECTED_PATHS` now supports **`re:`-prefixed regex** entries, tested against
  the resolved absolute deletion target. Anchor with `^…$` to protect a dir *exactly*
  (not its contents), or use alternation / negative-lookahead to include or exempt
  subtrees. Literal paths keep working; invalid regexes are skipped with a stderr warning.

### Changed
- Installer defaults tuned for a quiet, safe local setup: local mode
  (`AEGMIS_FORWARD_ALL=false`), `AEGMIS_PROTECTED_PATHS=re:^$HOME$` (gate the home dir
  itself, not everything under it), and — where the hook reads it — `AEGMIS_GATED_TOOLS`
  scoped to the shell tool only.
- README gains an entry-format table and worked-examples for `AEGMIS_PROTECTED_PATHS`;
  `.env.example` updated to match.

## [0.0.1] - 2026-07-11

Initial release.

### Added
- OpenAI Codex CLI `PreToolUse` hook (Python) that gates **Bash / apply_patch** behind a
  human Slack approval via the Aegmis intrupt API. Forward-all and local modes,
  fail-closed on reject/timeout/error, `policies.example.sh`, one-line `install.sh`, and
  offline smoke tests. Block signal: `hookSpecificOutput.permissionDecision: deny`.
- `AEGMIS_APPROVAL` — master kill switch (default `true`; set `false` to disable the gate
  entirely and allow everything).
- `AEGMIS_PROTECTED_PATHS` — comma-separated dirs to also gate `rm` on (the dir and
  everything under it), with **cwd-aware resolution** so relative targets (`./ok`, `ok`,
  `../x`) are caught, not just absolute paths.
- Catastrophic-only deletion gate: gates `rm` targeting the home dir, filesystem root, a
  `/Users/<name>` or `/home/<name>` home, a system dir (`/etc`, `/usr`, `/var`, …), or a
  bare `*` / `.` / `..`. Routine and project-local deletes (`rm file`,
  `rm -rf node_modules`, `rm -rf build/`) pass **without** approval.
- `policies.example.sh` documents the engine's **start-anchored** regex matching (prefix
  patterns with `[\s\S]*`) and ships a destructive-action reference table.

Configuration is via `AEGMIS_*` environment variables.
