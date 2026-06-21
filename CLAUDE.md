# CLAUDE.md

Project notes for working in the **creativity-graph** repo. See `README.md` for what the plugin
is and `ARCHITECTURE.md` for the data-model and boundary contract.

## Releasing

Maintainer checklist for cutting a public release. Everything up to step 4 is automated and
reproducible; **step 4 (public publish + tag) is an outward-facing action a human runs
deliberately** — it is intentionally not automated and not performed by tooling on your behalf.

### 1. Pre-flight (automated)

```sh
pip install -e ".[dev,backend]"
pytest tests/ -q                                   # full suite must be green
python -m kg_engine.pack validate pack/pack.yaml examples/source.md
python scripts/validate_plugin.py                  # manifests parse, components present, versions agree
claude plugin validate ./ --strict                 # the real validator, if the CLI is installed
```

CI (`.github/workflows/ci.yml`) runs the first four on every push/PR; `claude plugin validate
--strict` runs as a best-effort job (it needs the Claude Code CLI, which may be unavailable in a
generic runner — `scripts/validate_plugin.py` is the hard gate).

### 2. Bump the version

Set the **same** version string in both manifests (the structural validator enforces they agree):

- `.claude-plugin/plugin.json` → `version`
- `.claude-plugin/marketplace.json` → the `creativity-graph` entry's `version`

Follow SemVer. Update `CHANGELOG.md`: move items out of `[Unreleased]` under the new version.

### 3. Optional — refresh the graph headlessly (CI / no session)

The in-session path (`/kg-build`) needs no API keys. For an unattended rebuild (e.g. a release
artifact built in CI), use the headless backend with an `ANTHROPIC_API_KEY`:

```sh
export ANTHROPIC_API_KEY=sk-...
export KG_PROJECT_DIR=/path/to/vault
export KG_SOURCE_PATH=examples/source.md
export KG_PACK_PATH=pack/pack.yaml
python -m kg_engine.backend extract            # extract → boundary → canon → project
```

### 4. Publish + tag (manual, outward-facing — run by a human)

These steps push to an external marketplace and are **not** automated:

1. Commit the version bump and changelog on `main`.
2. Create the marketplace entry in the **public** marketplace repo (the bundled
   `.claude-plugin/marketplace.json` is a single-plugin **local/dev** marketplace for
   `--plugin-dir` installs, not the public listing).
3. Tag the release: `claude plugin tag creativity-graph <version>`.
4. `git tag vX.Y.Z && git push --tags`.

Publishing is hard to reverse and makes the release publicly installable — do it only when steps 1–3
are green and the version is final.
