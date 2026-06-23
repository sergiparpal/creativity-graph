#!/usr/bin/env python3
"""PreToolUse hook (§Stage 5): inject grounding-aware graph context on Grep/Glob/Read so the session
queries the graph first. Reads precomputed ranks O(1); never computes centrality. Fails silent."""
import json
import os
import pathlib
import sys

root = os.environ.get("CLAUDE_PLUGIN_ROOT")
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
    project = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
    if not project:
        return 0
    data = os.environ.get("CLAUDE_PLUGIN_DATA") or str(pathlib.Path(project) / ".kg-data")
    # Check the index exists BEFORE constructing the engine — this hook runs on every Grep/Glob/Read;
    # don't build context (or touch the derived tree) when nothing has been projected yet.
    if not (pathlib.Path(data) / "derived" / "index.sqlite").exists():
        return 0
    try:
        from kg_engine.canon import Canon
        from kg_engine.projector import Projector
        # ensure_layout=False: this is a pure read path, so don't let Canon() mkdir the canon dir or
        # rewrite .git/info/exclude on every single Grep/Glob/Read (the writer-side server already
        # maintains those). The derived dir is guaranteed to exist (index.sqlite checked above), so
        # Projector's own derived mkdir is a no-op here too.
        proj = Projector(Canon(project, ensure_layout=False), pathlib.Path(data) / "derived")
        if not proj.db_path.exists():
            return 0  # nothing projected yet
        # Mirror the server's lazy-reproject gate (server._ensure_projected): a raw
        # kg_context read off a stale projection would inject obsolete provenance /
        # epistemic labels. The index already exists (guarded above), so this is a cheap
        # incremental reproject, never a side-effecting cold build. Best-effort.
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
