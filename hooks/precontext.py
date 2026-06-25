#!/usr/bin/env python3
"""PreToolUse hook (§Stage 5): inject grounding-aware graph context on Grep/Glob/Read so the session
queries the graph first. Reads precomputed ranks O(1); never computes centrality. Fails silent."""
import json
import os
import pathlib
import sys


def _clean(value: "str | None") -> str:
    """Mirror bootstrap._clean / the launchers' clean(): drop empty, whitespace, unsubstituted
    ${...}, and the bare-sentinel results of substituting an empty ${...} into a ${VAR}/.venv
    template so an unset/unsubstituted env var never sends us to a bogus path (review-low)."""
    if not value:
        return ""
    v = value.strip()
    if not v or v.startswith("${") or v in ("/.venv", "/venv"):
        return ""
    return v


root = _clean(os.environ.get("CLAUDE_PLUGIN_ROOT"))
if root:
    sys.path.insert(0, str(pathlib.Path(root) / "scripts"))


def emit(ctx: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ctx}}))


def main() -> int:
    try:
        # Decode stdin as UTF-8 explicitly — json.load(sys.stdin) would use the locale text
        # encoding, so a non-ASCII payload mojibakes (empty kg_context match) or raises
        # UnicodeDecodeError on a non-UTF-8 locale (e.g. Windows cp1252), silently disabling
        # precontext for unicode payloads. Reading bytes makes the decode deterministic.
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        return 0
    # Resolve project/data with the SAME precedence the server uses (build_engine_from_env): KG_PROJECT_DIR
    # / KG_DATA (the .mcp.json override knobs) win over the CLAUDE_* defaults — else precontext reads a
    # DIFFERENT vault/derived dir than the server whenever those overrides are set (review-low).
    project = _clean(os.environ.get("KG_PROJECT_DIR")) or _clean(os.environ.get("CLAUDE_PROJECT_DIR")) \
        or payload.get("cwd")
    if not project:
        return 0
    data = _clean(os.environ.get("KG_DATA")) or _clean(os.environ.get("CLAUDE_PLUGIN_DATA")) \
        or str(pathlib.Path(project) / ".kg-data")
    # Check the index exists BEFORE constructing the engine — this hook runs on every Grep/Glob/Read;
    # don't build context (or touch the derived tree) when nothing has been projected yet.
    if not (pathlib.Path(data) / "derived" / "index.sqlite").exists():
        return 0
    # Resolve source / pack / metrics_mode with the SAME precedence + project-relative defaults the
    # server uses (build_engine_from_env), so the hook's projection wires the IDENTICAL IDF corpus,
    # specificity seeds, and metrics_mode — never a degraded empty-corpus derived layer the server would
    # then serve as fresh (finding: precontext-bypasses-facade). KG_* live only in the MCP server's
    # .mcp.json env; in the hook env these resolve via CLAUDE_PLUGIN_OPTION_* + the documented defaults.
    source = _clean(os.environ.get("CLAUDE_PLUGIN_OPTION_SOURCE_PATH")) or _clean(os.environ.get("KG_SOURCE_PATH"))
    if not source:
        guess = pathlib.Path(project) / "examples" / "source.md"
        source = str(guess) if guess.exists() else None
    pack_path = _clean(os.environ.get("KG_PACK_PATH"))
    if not pack_path:
        guess = pathlib.Path(project) / "pack" / "pack.yaml"
        pack_path = str(guess) if guess.exists() else None
    metrics_mode = (os.environ.get("CLAUDE_PLUGIN_OPTION_METRICS_MODE") or "").strip() or "structure_only"
    try:
        from kg_engine.server import KGEngine
        # read_only_projector wires source/pack/metrics through the SAME seam as the server AND keeps a
        # no-side-effect Canon(ensure_layout=False): a pure read path that never mkdir's the canon dir or
        # rewrites .git/info/exclude on a plain Grep/Glob/Read (the writer-side server maintains those).
        # The derived dir exists (index.sqlite checked above), so the projector's own mkdir is a no-op.
        proj = KGEngine.read_only_projector(project, data, source_path=source, pack_path=pack_path,
                                            metrics_mode=metrics_mode)
        if not proj.db_path.exists():
            return 0  # nothing projected yet
        # Mirror the server's lazy-reproject gate (server._ensure_projected): a raw kg_context read off a
        # stale projection would inject obsolete provenance / epistemic labels. The index already exists
        # (guarded above), so this is a cheap incremental reproject, never a side-effecting cold build.
        if proj.is_stale():
            proj.project()
        ti = payload.get("tool_input", {})
        query = ti.get("pattern") or ti.get("query") \
            or (pathlib.Path(ti.get("file_path", "")).stem or None)
        ctx = proj.kg_context(query, budget=800)
        if not ctx["items"] and not ctx["advisory"]["nodes"]:
            return 0
        lines = ["creativity-graph (query the graph first; provenance + falsification attached):"]
        for it in ctx["items"][:6]:
            lines.append(f"- {it['source']} --{it['relation']}--> {it['target']} "
                         f"[{it['provenance']}/{it['epistemic_state']}]")
        fc = ctx["falsification_counters"]["failed_or_rejected_edges"]
        if fc:
            lines.append(f"- {fc} falsified/rejected edge(s) on record (memory of failures)")
        if ctx["advisory"]["nodes"]:
            br = ", ".join(n["label"] for n in ctx["advisory"]["nodes"][:3])
            lines.append(f"- structural-bridge advisory (heuristic, NOT a guarantee): {br}")
        emit("\n".join(lines))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
