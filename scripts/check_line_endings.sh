#!/usr/bin/env bash
# Fail if a file that must be LF has a carriage return in the working tree.
#
# Why this exists. `set -euo pipefail` with a trailing CR becomes `pipefail\r`,
# which bash rejects as an invalid option name, and the script dies on its first
# line inside the container with a message that explains nothing. That happened
# while building this repository: scripts/pki.sh was written with CRLF by a
# Windows tool, Compose bind-mounted it straight into a Linux container, and the
# PKI job failed with "set: pipefail: invalid option name".
#
# Why the *working tree* and not the index. Git normalises text files to LF in
# the index, always, so the index is clean by construction and checking it tests
# nothing. The failure above never went through git at all: the file was read off
# disk through a bind mount. The working tree is where the problem lives.
#
# Which files. Whatever .gitattributes says must be LF. Those get LF on checkout
# on every platform, including Windows with core.autocrlf=true, because an
# explicit eol attribute overrides autocrlf. So a CR in one of them is always a
# local accident and never a legitimate platform difference. Files declared
# eol=crlf, such as the .bat helpers, are skipped by construction.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

offenders=()

# git check-attr --stdin answers for every tracked file in one invocation, which
# matters: one process per file across 170 files is slow enough to notice in CI.
# With -z it emits NUL-separated triples of path, attribute name and value, so
# the loop reads three records per file.
while IFS= read -r path && IFS= read -r _attr && IFS= read -r value; do
  [[ "$value" == "lf" ]] || continue
  [[ -f "$path" ]] || continue

  # -U stops grep stripping CR itself on platforms where it treats the file as
  # text, which would make the check silently always pass.
  if LC_ALL=C grep -qU $'\r' -- "$path"; then
    offenders+=("$path")
  fi
done < <(git ls-files -z | git check-attr --stdin -z eol 2>/dev/null | tr '\0' '\n')

if [[ ${#offenders[@]} -gt 0 ]]; then
  printf 'Files that must be LF but contain carriage returns:\n\n' >&2
  printf '  %s\n' "${offenders[@]}" >&2
  printf '\n' >&2
  cat >&2 <<'MESSAGE'
A CR in a shell script breaks it the moment a Linux container runs it, with an
error that points nowhere useful. A CR in a generated file breaks the freshness
check in CI.

To fix, in the repository root:

  python - <<'PY'
  import subprocess
  from pathlib import Path
  out = subprocess.run(["git","ls-files"], capture_output=True, text=True).stdout
  for name in out.split():
      p = Path(name)
      if p.is_file():
          raw = p.read_bytes()
          if b"\r\n" in raw:
              p.write_bytes(raw.replace(b"\r\n", b"\n"))
              print("fixed", name)
  PY

Note that Python's Path.write_text translates to CRLF on Windows unless passed
newline="". The generators in scripts/ pass it for exactly this reason.
MESSAGE
  exit 1
fi

echo "line endings are clean: every file declared eol=lf is LF in the working tree"
