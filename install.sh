#!/usr/bin/env bash
set -euo pipefail

# ─── Central Brain Installer ────────────────────────────────────────────────
# Installs central-brain MCP Memory Server for Claude Code.
# Safe to re-run (idempotent). Creates backups before modifying config files.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_DIR="$HOME/.claude"
MCP_JSON="$CLAUDE_DIR/.mcp.json"
SETTINGS_JSON="$CLAUDE_DIR/settings.json"
DATA_DIR="$HOME/.central-brain"

# ─── Helpers ─────────────────────────────────────────────────────────────────

info()  { printf '\033[1;34m[info]\033[0m  %s\n' "$*"; }
ok()    { printf '\033[1;32m[ok]\033[0m    %s\n' "$*"; }
warn()  { printf '\033[1;33m[warn]\033[0m  %s\n' "$*"; }
die()   { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

backup_file() {
    local f="$1"
    if [[ -f "$f" ]]; then
        cp "$f" "${f}.bak"
        info "Backed up $f → ${f}.bak"
    fi
}

# ─── Resolve SCRIPT_DIR (works for local run and symlinks) ───────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# If piped via curl|bash, SCRIPT_DIR won't contain pyproject.toml.
# In that case, clone the repo to a temp dir.
if [[ ! -f "$SCRIPT_DIR/pyproject.toml" ]]; then
    warn "pyproject.toml not found at $SCRIPT_DIR — assuming curl|bash install"
    TMPDIR="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR"' EXIT
    info "Cloning central-brain repo to $TMPDIR ..."
    git clone --depth 1 https://github.com/ajitesh-bhalerao/central-brain.git "$TMPDIR/central-brain"
    SCRIPT_DIR="$TMPDIR/central-brain"
fi

# ─── 1. Check Python >= 3.11 ────────────────────────────────────────────────

info "Checking Python version ..."

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        major="${ver%%.*}"
        minor="${ver##*.}"
        if (( major == 3 && minor >= 11 )); then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [[ -n "$PYTHON" ]]; then
    ok "Found $PYTHON ($ver)"
else
    warn "No Python >= 3.11 found on PATH (uv will download one automatically)"
fi

# ─── 2. Install uv if not present ───────────────────────────────────────────

if command -v uv &>/dev/null; then
    ok "uv already installed ($(uv --version))"
else
    info "Installing uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the env so uv is on PATH for the rest of this script
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if command -v uv &>/dev/null; then
        ok "uv installed ($(uv --version))"
    else
        die "uv installation failed — check output above"
    fi
fi

# ─── 3. Install central-brain as a uv tool ──────────────────────────────────

info "Installing central-brain via uv tool ..."
uv tool install --force --python ">=3.11" "$SCRIPT_DIR"
ok "central-brain installed"

# Ensure uv tool bin is on PATH for verification below
export PATH="$HOME/.local/bin:$PATH"

# ─── 4. Merge MCP config into ~/.claude/.mcp.json ───────────────────────────

info "Configuring MCP server ..."
mkdir -p "$CLAUDE_DIR"
backup_file "$MCP_JSON"

python3 -c "
import json, os, sys

path = sys.argv[1]
existing = {}
if os.path.isfile(path):
    with open(path) as f:
        existing = json.load(f)

servers = existing.setdefault('mcpServers', {})
servers['central-brain'] = {'command': 'central-brain', 'args': ['serve']}

with open(path, 'w') as f:
    json.dump(existing, f, indent=2)
    f.write('\n')
" "$MCP_JSON"

ok "MCP config written to $MCP_JSON"

# ─── 5. Merge hooks into ~/.claude/settings.json ────────────────────────────

info "Configuring hooks ..."
backup_file "$SETTINGS_JSON"

python3 -c "
import json, os, sys

path = sys.argv[1]
existing = {}
if os.path.isfile(path):
    with open(path) as f:
        existing = json.load(f)

hooks = existing.setdefault('hooks', {})

desired = {
    'SessionStart': {'command': 'central-brain hook-session-start'},
    'PreCompact':   {'command': 'central-brain hook-pre-compact'},
    'SessionEnd':   {'command': 'central-brain hook-stop'},
}

for event, hook_def in desired.items():
    entries = hooks.get(event, [])

    # Remove any existing central-brain entries (dedup on re-run)
    cleaned = []
    for entry in entries:
        inner_hooks = entry.get('hooks', [])
        filtered = [h for h in inner_hooks if 'central-brain' not in h.get('command', '')]
        if filtered:
            entry['hooks'] = filtered
            cleaned.append(entry)

    # Add fresh entry
    cleaned.append({
        'matcher': '',
        'hooks': [{'type': 'command', 'command': hook_def['command']}]
    })
    hooks[event] = cleaned

with open(path, 'w') as f:
    json.dump(existing, f, indent=2)
    f.write('\n')
" "$SETTINGS_JSON"

ok "Hooks written to $SETTINGS_JSON"

# ─── 6. Create data directory ───────────────────────────────────────────────

mkdir -p "$DATA_DIR"
ok "Data directory ready at $DATA_DIR"

# ─── 7. Optional: Voyage API key ────────────────────────────────────────────

if [[ -t 0 ]]; then
    # Interactive terminal — offer to set VOYAGE_API_KEY
    if [[ -z "${VOYAGE_API_KEY:-}" ]]; then
        echo ""
        info "Central Brain supports semantic search via VoyageAI embeddings (optional)."
        info "Get a key at https://dash.voyageai.com/api-keys"
        printf '       Enter VOYAGE_API_KEY (or press Enter to skip): '
        read -r voyage_key
        if [[ -n "$voyage_key" ]]; then
            # Append to shell profile
            SHELL_RC="$HOME/.zshrc"
            [[ "$SHELL" == */bash ]] && SHELL_RC="$HOME/.bashrc"
            echo "" >> "$SHELL_RC"
            echo "export VOYAGE_API_KEY=\"$voyage_key\"" >> "$SHELL_RC"
            export VOYAGE_API_KEY="$voyage_key"
            ok "VOYAGE_API_KEY added to $SHELL_RC (restart shell or source it)"
        else
            info "Skipped — FTS5 search will still work without embeddings"
        fi
    else
        ok "VOYAGE_API_KEY already set"
    fi
else
    info "Non-interactive mode — skipping VOYAGE_API_KEY prompt"
fi

# ─── 8. Verify installation ─────────────────────────────────────────────────

echo ""
info "Verifying installation ..."

CB_PATH="$(command -v central-brain 2>/dev/null || true)"
if [[ -n "$CB_PATH" ]]; then
    ok "central-brain binary: $CB_PATH"
else
    warn "central-brain not found on PATH — you may need to restart your shell"
fi

# Quick smoke test
if central-brain search "test" &>/dev/null; then
    ok "central-brain runs successfully"
else
    warn "central-brain smoke test failed — check installation"
fi

# ─── Summary ────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Central Brain installed successfully!"
echo ""
echo " MCP config:  $MCP_JSON"
echo " Hooks:       $SETTINGS_JSON"
echo " Data dir:    $DATA_DIR"
[[ -n "$CB_PATH" ]] && echo " Binary:      $CB_PATH"
echo ""
echo " Restart Claude Code to activate the MCP server."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
