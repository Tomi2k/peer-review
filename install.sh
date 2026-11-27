#!/usr/bin/env bash
# Installs Peer Review runtime, CLI wrappers, and SKILL.md for every supported agent CLI found.
#
# Usage: ./install.sh [--force] | --uninstall
#
#   peer_review/ + bin/ -> ~/.local/share/peer-review/
#   wrappers            -> ~/.local/bin/peer-review, ~/.local/bin/peer-review-update
#   SKILL.md            -> ~/.claude/skills/peer-review/
#                          ${CODEX_HOME:-~/.codex}/skills/peer-review/
#                          ~/.gemini/config/skills/peer-review/
#                          ${GROK_HOME:-~/.grok}/skills/peer-review/
#   example config      -> ~/.config/peer-review/config.json (only if absent, mode 0600)
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_SKILL="$ROOT/skills/peer-review/SKILL.md"
DEST="$HOME/.local/share/peer-review"
BIN_DIR="$HOME/.local/bin"
CFG_DIR="$HOME/.config/peer-review"
STATE_DIR="$HOME/.local/state/peer-review"
MANIFEST="$DEST/manifest.txt"

MODE=install
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --uninstall) MODE=uninstall ;;
    *) echo "usage: ./install.sh [--force] | --uninstall" >&2; exit 2 ;;
  esac
done

[ -f "$SRC_SKILL" ] || { echo "ERROR: run this from the peer-review repository root" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required (3.10+)" >&2; exit 2; }

for p in \
  "$HOME/.local" "$HOME/.local/share" "$HOME/.local/state" "$HOME/.config" \
  "$DEST" "$DEST/peer_review" "$DEST/bin" "$MANIFEST" \
  "$BIN_DIR" "$BIN_DIR/peer-review" "$BIN_DIR/peer-review-update" \
  "$CFG_DIR" "$CFG_DIR/config.json" "$STATE_DIR"
do
  if [ -L "$p" ]; then
    echo "ERROR: $p is a symlink — remove it first" >&2
    exit 2
  fi
done

sha() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    sha256sum "$1" | cut -d' ' -f1
  fi
}

ours() { [ -f "$MANIFEST" ] && grep -Fxq -- "$(sha "$1")  $1" "$MANIFEST"; }

targets() {
  echo "claude $HOME/.claude/skills"
  echo "codex ${CODEX_HOME:-$HOME/.codex}/skills"
  echo "agy $HOME/.gemini/config/skills"
  echo "grok ${GROK_HOME:-$HOME/.grok}/skills"
}

TMP_PATHS=()
OLD_BACKUP=""
SWAP_DONE=0
cleanup() {
  if [ "$SWAP_DONE" = "0" ] && [ -n "$OLD_BACKUP" ] && [ -d "$OLD_BACKUP" ]; then
    [ -d "$OLD_BACKUP/peer_review" ] && [ ! -e "$DEST/peer_review" ] && mv "$OLD_BACKUP/peer_review" "$DEST/" || true
    [ -d "$OLD_BACKUP/bin" ] && [ ! -e "$DEST/bin" ] && mv "$OLD_BACKUP/bin" "$DEST/" || true
  fi
  for p in "${TMP_PATHS[@]:-}"; do
    if [ -n "$p" ]; then
      rm -rf -- "$p"
    fi
  done
}
trap cleanup EXIT

if [ "$MODE" = "uninstall" ]; then
  if [ -f "$MANIFEST" ]; then
    while IFS= read -r line; do
      f="${line#*  }"
      case "$f" in
        */peer-review/SKILL.md) ;;
        *) echo "skipped  $f (unexpected manifest path)"; continue ;;
      esac
      if [ -L "$f" ] || [ -L "$(dirname "$f")" ]; then
        echo "kept     $f (symlink)"
        continue
      fi
      [ -f "$f" ] || continue
      if ours "$f"; then
        rm -f -- "$f"
        rmdir -- "$(dirname "$f")" 2>/dev/null || true
        echo "removed  $f"
      else
        echo "kept     $f (modified since install)"
      fi
    done < "$MANIFEST"
  fi
  for w in "$BIN_DIR/peer-review" "$BIN_DIR/peer-review-update"; do
    if [ -f "$w" ] && [ ! -L "$w" ]; then
      if grep -q "peer_review/cli.py" "$w" 2>/dev/null; then
        rm -f -- "$w"
        echo "removed  $w"
      fi
    fi
  done
  rm -rf -- "$DEST/peer_review" "$DEST/bin" "$MANIFEST"
  rmdir -- "$DEST" 2>/dev/null && echo "removed  $DEST" || echo "kept     $DEST (other files inside)"
  exit 0
fi

# Atomic stage and swap of Python runtime (prevents half-installed broken window).
mkdir -p "$DEST" "$BIN_DIR" "$CFG_DIR" "$STATE_DIR"
chmod 700 "$DEST" "$CFG_DIR" "$STATE_DIR"

STAGE="$(mktemp -d "$DEST/.stage.XXXXXX")"
TMP_PATHS+=("$STAGE")
cp -R "$ROOT/peer_review" "$ROOT/bin" "$STAGE/"
find "$STAGE" -name '__pycache__' -prune -exec rm -rf {} +
find "$STAGE" -name '*.pyc' -delete
chmod 755 "$STAGE/bin/peer-review" "$STAGE/bin/peer-review-update"

OLD_BACKUP=""
if [ -d "$DEST/peer_review" ] || [ -d "$DEST/bin" ]; then
  OLD_BACKUP="$(mktemp -d "$DEST/.old.XXXXXX")"
  TMP_PATHS+=("$OLD_BACKUP")
  [ -d "$DEST/peer_review" ] && mv "$DEST/peer_review" "$OLD_BACKUP/"
  [ -d "$DEST/bin" ] && mv "$DEST/bin" "$OLD_BACKUP/"
fi
mv "$STAGE/peer_review" "$STAGE/bin" "$DEST/"
SWAP_DONE=1
echo "runtime  $DEST"

# Install isolated CLI wrappers atomically via mktemp + mv.
printf -v Q_DEST '%q' "$DEST"
WRAP1="$(mktemp "$BIN_DIR/.peer-review.XXXXXX")"
WRAP2="$(mktemp "$BIN_DIR/.peer-review-update.XXXXXX")"
TMP_PATHS+=("$WRAP1" "$WRAP2")
cat > "$WRAP1" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec python3 -I ${Q_DEST}/peer_review/cli.py "\$@"
EOF
cat > "$WRAP2" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec python3 -I ${Q_DEST}/peer_review/cli.py update "\$@"
EOF
chmod 755 "$WRAP1" "$WRAP2"
mv "$WRAP1" "$BIN_DIR/peer-review"
mv "$WRAP2" "$BIN_DIR/peer-review-update"
echo "wrappers $BIN_DIR/peer-review, $BIN_DIR/peer-review-update"

# Install SKILL.md into every installed agent CLI with exact manifest tracking.
NEW_MANIFEST="$(mktemp "$DEST/.manifest.XXXXXX")"
TMP_PATHS+=("$NEW_MANIFEST")
[ -f "$MANIFEST" ] && cp "$MANIFEST" "$NEW_MANIFEST"
while read -r cli dir; do
  if ! command -v "$cli" >/dev/null 2>&1; then
    echo "skipped  $cli: not installed"
    continue
  fi
  f="$dir/peer-review/SKILL.md"
  if [ -L "$(dirname "$dir")" ] || [ -L "$dir" ] || [ -L "$dir/peer-review" ] || [ -L "$f" ]; then
    echo "skipped  $cli: $f or parent is a symlink"
    continue
  fi
  if [ -e "$f" ] && ! ours "$f" && [ "$FORCE" != "1" ]; then
    echo "skipped  $cli: $f exists and was not installed by peer-review (--force replaces it)"
    continue
  fi
  mkdir -p "$dir/peer-review"
  if [ -e "$f" ] && ! ours "$f"; then
    cp -p "$f" "$f.bak-$(date -u +%Y%m%d-%H%M%S)"
    echo "backup   $f.bak-*"
  fi
  SKILL_TMP="$(mktemp "$dir/peer-review/.SKILL.XXXXXX")"
  TMP_PATHS+=("$SKILL_TMP")
  cp "$SRC_SKILL" "$SKILL_TMP"
  chmod 644 "$SKILL_TMP"
  mv "$SKILL_TMP" "$f"
  FILTERED="$(mktemp "$DEST/.manifest-filter.XXXXXX")"
  TMP_PATHS+=("$FILTERED")
  awk -v target="$f" 'index($0, "  ") > 0 && substr($0, index($0, "  ") + 2) == target { next } { print }' "$NEW_MANIFEST" > "$FILTERED"
  mv "$FILTERED" "$NEW_MANIFEST"
  echo "$(sha "$f")  $f" >> "$NEW_MANIFEST"
  echo "skill    $cli: $f"
done < <(targets)
chmod 600 "$NEW_MANIFEST"
mv "$NEW_MANIFEST" "$MANIFEST"

# Seed example config only if the user has no config yet (symlink already refused above).
if [ ! -e "$CFG_DIR/config.json" ]; then
  CFG_TMP="$(mktemp "$CFG_DIR/.config.XXXXXX")"
  TMP_PATHS+=("$CFG_TMP")
  cp "$ROOT/examples/config.api.example.json" "$CFG_TMP"
  chmod 600 "$CFG_TMP"
  mv "$CFG_TMP" "$CFG_DIR/config.json"
  echo "config   $CFG_DIR/config.json (seeded from examples/config.api.example.json — edit before first run)"
else
  echo "config   $CFG_DIR/config.json (kept existing)"
fi

echo ""
echo "Peer Review installed. Next steps:"
echo "  1. Edit $CFG_DIR/config.json (choose API keys or local CLI backends)."
echo "  2. Export FIRECRAWL_API_KEY and your provider API keys (or sign into your CLIs)."
echo "  3. Initialize your lineup: peer-review-update"
echo "  4. Run a review: peer-review run --task \"Review this change\" --file \"./path/to/file.py\""
