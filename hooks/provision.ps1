# SessionStart launcher (Windows PowerShell — for native Windows).
#
# Thin launcher: find a Python >= 3.10 and hand off to scripts/bootstrap.py, which does
# the real, idempotent, concurrency-safe provisioning in a detached background process.
# All the heavy logic lives in bootstrap.py.
#
# Invoked by hooks/provision.mjs (the cross-platform SessionStart dispatcher, run with
# "async": true) on native Windows; provision.sh covers POSIX platforms. Also runnable
# directly by a developer.
$ErrorActionPreference = 'SilentlyContinue'

$root = $env:CLAUDE_PLUGIN_ROOT
if (-not $root) {
  # Dev fallback: this script lives in hooks/, so the repo root is its grandparent.
  $root = Split-Path -Parent $PSScriptRoot
}

# Prepend uv's usual install dir so a standalone-installed uv that is not yet on the inherited process
# PATH is still found by bootstrap's `shutil.which('uv')` and the faster `uv sync` path is taken — the
# parity mirror of provision.sh's $HOME/.local/bin prepend. Harmless if the dir is absent.
if ($env:USERPROFILE) { $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH" }

$boot = Join-Path $root 'scripts/bootstrap.py'
if (-not (Test-Path $boot)) { exit 0 }

# NB: no pointer-only fast path here. The interpreter pointer (engine-python.txt)
# survives a plugin update that changes dependencies, so short-circuiting on its
# existence would skip the background rebuild on exactly the update case. Instead always
# hand off to `bootstrap.py --background`, which checks the content STAMP (the real
# freshness gate) and returns in milliseconds when current.

# Find a Python >= 3.10. `py` (the Windows launcher) and `python` come first.
$py = $null
foreach ($cand in @('py', 'python', 'python3')) {
  if (Get-Command $cand -ErrorAction SilentlyContinue) {
    & $cand -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $cand; break }
  }
}

# No suitable Python: stay silent (background hook). The MCP launcher surfaces a clear,
# actionable message the first time the server is spawned.
if (-not $py) { exit 0 }

& $py $boot --background
exit 0
