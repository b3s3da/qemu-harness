#!/usr/bin/env bash
# qh installer (Linux / macOS).  GPL-3.0-or-later.
#   ./install.sh [--arch aarch64|arm|riscv64|x86_64|all] [--no-path] [--no-skills] [--smoke]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH=aarch64; NOPATH=0; NOSKILLS=0; SMOKE=0
while [ $# -gt 0 ]; do case "$1" in
  --arch) ARCH="$2"; shift;; --no-path) NOPATH=1;; --no-skills) NOSKILLS=1;; --smoke) SMOKE=1;;
  *) echo "unknown option $1"; exit 2;; esac; shift; done
ok(){ printf '\033[32m[ok]\033[0m   %s\n' "$*"; }; fail(){ printf '\033[31m[fail]\033[0m %s\n' "$*"; exit 1; }

command -v python3 >/dev/null || fail "python3 not found (apt install python3 / brew install python)"
python3 -c 'import sys; sys.exit(sys.version_info < (3,8))' || fail "need Python >= 3.8"
ok "python $(python3 -V 2>&1 | cut -d' ' -f2)"
ARCHS=$([ "$ARCH" = all ] && echo "aarch64 arm riscv64 x86_64" || echo "$ARCH")
for a in $ARCHS; do command -v "qemu-system-$a" >/dev/null || fail "qemu-system-$a not found (apt install qemu-system / brew install qemu)"; done
ok "qemu present for: $ARCHS"
command -v go >/dev/null || fail "Go not found (needed to build the guest agent): https://go.dev/dl/"
ok "$(go version)"
for a in $ARCHS; do python3 "$ROOT/qh.py" setup --arch "$a"; done
ok "guest userland + agent ready"

if [ "$NOPATH" = 0 ]; then
  mkdir -p "$HOME/.local/bin"
  printf '#!/bin/sh\nexec python3 "%s/qh.py" "$@"\n' "$ROOT" > "$HOME/.local/bin/qh"; chmod +x "$HOME/.local/bin/qh"
  ok "installed ~/.local/bin/qh (make sure ~/.local/bin is on PATH)"
fi
if [ "$NOSKILLS" = 0 ]; then
  for d in "$HOME/.claude/skills" "$HOME/.grok/skills"; do
    mkdir -p "$d/qemu-harness"; cp "$ROOT/skill/SKILL.md" "$d/qemu-harness/SKILL.md"; ok "skill -> $d/qemu-harness"
  done
fi
if [ "$SMOKE" = 1 ]; then
  T="$(mktemp -d)"; trap 'python3 "$ROOT/qh.py" down >/dev/null 2>&1 || true; rm -rf "$T"' EXIT; cd "$T"
  echo "{\"kernel\":\"alpine:${ARCH%% *}\",\"fwd\":[]}" > qh.json
  python3 "$ROOT/qh.py" up --timeout 120 && python3 "$ROOT/qh.py" exec 'uname -a' && ok "smoke test passed"
fi
echo "Done. Try:  mkdir lab && cd lab && qh up --kernel alpine:${ARCH%% *} && qh exec 'uname -a' && qh down"
