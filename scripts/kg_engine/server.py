"""The MCP server (§2.4): the graphify-shaped tool surface + our grounding semantics.

Tool logic lives in the importable `KGEngine` facade so it is unit-testable without an MCP client;
the FastMCP wrappers are thin. Elicitation requests always declare a default applied if unanswered,
so the flow never stalls (§2.4, §4).
"""
from __future__ import annotations

import contextlib
import copy
import functools
import hashlib
import json
import logging
from collections import Counter, OrderedDict
import math
import os
import re
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .boundary import DEFAULT_MAX_EDGES_PER_KB, MIN_SPAN_CHARS, merge_results_into_nodes, validate_payload
from .canon import Canon
from .groundaudit import GroundAuditLog
from .model import (
    AuthoredBy,
    Disposition,
    Edge,
    EpistemicState,
    GROUNDABLE_STATES,
    Node,
    Provenance,
    UNDECLARED_TYPE,
    normalize_text,
    slug,
    utcnow,
)
from .pack import load_pack
from .projector import Projector
from .reconciler import GROUND_AUDIT, Reconciler
from .scrub import Scrubber
from .sources import SourceSet

# Module logger. The engine had no logging seam, so silent `except Exception: pass` fallbacks were
# invisible to an operator. Stays quiet by default (no handler attached) per the library convention; an
# operator opts in via standard logging config. The MCP tool envelope and the index fallbacks log here.
logger = logging.getLogger("kg_engine")

# Single source of truth shared with the reconciler's policed set (model.GROUNDABLE_STATES), so the
# states kg_ground may stamp and the states the reconciler re-quarantines can never drift apart.
VALID_VERDICTS = {s.value for s in GROUNDABLE_STATES}
# The known verdict actors, derived from the AuthoredBy enum (mirroring how VALID_VERDICTS derives from
# GROUNDABLE_STATES) so the clamp tracks the model instead of an inline literal that can drift.
VALID_ACTORS = {a.value for a in AuthoredBy}

# Absolute filesystem paths (Windows drive/UNC, or a POSIX path of >=2 segments) — redacted from any
# error string before it crosses the §1.9 egress boundary back to the session, so a raw exception can't
# leak a vault path. A bare "/" or a single-segment "/x" or a mid-word "and/or" is deliberately NOT
# matched (over-redaction of prose), only path-shaped runs.
_ABS_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`]*"        # C:\... / C:/... drive path, or \\server\share UNC
    r"|/(?:[^\s\"'`/]+/)+[^\s\"'`/]*")            # POSIX absolute path (>=2 segments)


def _scrub_error_text(msg, *, sensitivity: str = "medium") -> str:
    """Scrub an error string before it crosses the §1.9 egress boundary back to the session: redact
    absolute filesystem paths AND run the same secret/PII egress scrub `kg_scrub` applies, so a raw
    exception can't leak a vault path or quote un-scrubbed canon content. Uses a THROWAWAY `Scrubber`
    (the session's accumulated egress placeholder namespace is never polluted by error text) and degrades
    to the best partial result if scrubbing itself raises — the error-reporting path must NEVER itself
    raise. This is the single chokepoint both the tool envelope and the handler `{e}` interpolations route
    through."""
    try:
        text = str(msg)
    except Exception:  # noqa: BLE001 — an un-str-able error must not crash the error path
        return "<unprintable error>"
    try:
        text = _ABS_PATH_RE.sub("<path>", text)
    except Exception:  # noqa: BLE001 — path redaction is best-effort
        pass
    try:
        text = Scrubber(sensitivity).scrub(text)[0]
    except Exception:  # noqa: BLE001 — the secret/PII scrub is best-effort; keep the path-redacted text
        pass
    return text

# Precedence used when kg_merge dedups two edges that collide on one canonical id (§1.4/§1.7). The
# winning epistemic_state is whichever ranks higher: failed/rejected are sticky NEGATIVE INFORMATION
# (never pruned, §1.7) so they dominate any positive state; then grounded > unverified; `obsolete`
# (a lifecycle state, not a verdict) ranks lowest. The merged state is therefore ALWAYS a state one of
# the two real edges already held — the merge can never forge or upgrade a verdict.
_MERGE_STATE_RANK = {
    EpistemicState.FAILED: 4,
    EpistemicState.REJECTED: 3,
    EpistemicState.GROUNDED: 2,
    EpistemicState.UNVERIFIED: 1,
    EpistemicState.OBSOLETE: 0,
}
# Tie-break (and span-less provenance) order: a verbatim span (span-present) beats an asserted
# inference, which beats a structural guess (hypothesized). Used to keep the cited span + its verdict
# paired on a state tie, never to invent evidence.
_MERGE_PROV_RANK = {
    Provenance.SPAN_PRESENT: 2,
    Provenance.INFERRED: 1,
    Provenance.HYPOTHESIZED: 0,
}

# ---- transport/cancellation hardening (the robustness pass) ----------------------------------------
# A rotating server log so the whole class of stdio-transport/cancellation crash is finally debuggable:
# nothing was persisted before, so a server that "disconnected" left no trace. Lives under KG_DATA next
# to provision.log; the Node supervisor (launch_server.mjs) appends its (re)launch/exit/backoff events to
# the SAME file (Python owns rotation — the supervisor's lines are a few per restart, so they ride along).
SERVER_LOG_NAME = "server.log"
SERVER_LOG_MAX_BYTES = 2_000_000
SERVER_LOG_BACKUP_COUNT = 3
# Distinct non-zero exit codes so the supervisor's logs say WHY the engine died (both are "unexpected",
# so both trigger a relaunch — see launch_server.mjs restartDecision).
EXIT_CRASH = 70         # an exception escaped the serve loop
EXIT_WATCHDOG = 71      # a handler wedged past KG_HANDLER_TIMEOUT and the watchdog forced a fresh process
# A handler that runs longer than this is treated as wedged (a deadlocked write, a runaway projection) and
# the watchdog forces a clean process exit so the supervisor relaunches — never a half-dead "Running…"
# state. Generous by default: the only legitimately-slow path is a cross-process lease wait (capped at
# canon.LOCK_ACQUIRE_TIMEOUT = 30s) plus a projection, both far under this. 0 disables the watchdog.
DEFAULT_HANDLER_TIMEOUT = 300.0
# Idempotency: bound the in-memory replay cache so a long-lived server can't grow it without limit.
_WRITE_CACHE_MAX = 256


def _clean_env(key: str) -> str | None:
    """Read an env var, treating empty OR an unsubstituted ``${...}`` placeholder as unset (mirrors
    ``bootstrap._clean`` / ``launch_server.clean``). Lifted to module scope so the logging path and
    ``build_engine_from_env`` resolve project/data/source identically."""
    v = (os.environ.get(key) or "").strip()
    return None if not v or v.startswith("${") else v


def resolve_data_dir() -> Path:
    """The engine data dir (where the derived layer + server.log live), resolved exactly as a
    KGEngine would: ``KG_DATA`` if set, else ``<project>/.kg-data``. Used to place the server log
    BEFORE the engine is constructed, so even an engine-construction error is captured."""
    proj = _clean_env("KG_PROJECT_DIR") or _clean_env("CLAUDE_PROJECT_DIR") or os.getcwd()
    data = _clean_env("KG_DATA")
    return Path(data) if data else (Path(proj) / ".kg-data")


def server_log_path(data_dir=None) -> Path:
    return (Path(data_dir) if data_dir else resolve_data_dir()) / SERVER_LOG_NAME


# A readiness MARKER the Node supervisor (launch_server.mjs) reads to tell a POST-INIT crash from a
# STARTUP crash without relying solely on a wall-clock proxy: it is written as the stdio serve loop comes
# up (the lifespan __aenter__, AFTER imports + engine construction succeed) and its mtime, newer than the
# child's spawn time, proves THIS engine began serving. Lives under the SAME KG_DATA dir both sides resolve
# (resolve_data_dir <-> serverLogDir in the launcher).
READY_MARKER_NAME = ".engine-ready"


def ready_marker_path(data_dir=None) -> Path:
    return (Path(data_dir) if data_dir else resolve_data_dir()) / READY_MARKER_NAME


def write_ready_marker(data_dir=None) -> None:
    """Stamp the readiness marker (best-effort; a failure must never block serving)."""
    try:
        p = ready_marker_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"pid={os.getpid()} t={time.time():.3f}\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 — the marker is advisory; never fail startup on it
        pass


def clear_ready_marker(data_dir=None) -> None:
    """Remove the readiness marker on a clean shutdown (best-effort). The supervisor also clears it on
    each (re)spawn, so a leftover from a hard crash can never be mistaken for the next child's marker."""
    try:
        ready_marker_path(data_dir).unlink()
    except OSError:
        pass


@contextlib.asynccontextmanager
async def readiness_lifespan(_server=None):
    """FastMCP lifespan: write the readiness marker as the stdio serve loop starts, clear it on exit.

    __aenter__ runs after the transport is established and right BEFORE the session begins reading the
    buffered ``initialize`` request — and crucially AFTER module import + ``build_engine_from_env``, so a
    broken-venv import/construction error (a genuine STARTUP failure) never reaches here and stays
    correctly classified by the supervisor's wall-clock fallback. RESIDUAL GAP: the few milliseconds
    between this point and ``initialize`` actually being answered are attributed to "serving", so a crash
    in that sliver is treated as post-init — acceptable since no engine code runs there (MCP handles the
    handshake) and the alternative (a true on-initialize hook) does not exist in this FastMCP."""
    write_ready_marker()
    try:
        yield {}
    finally:
        clear_ready_marker()


_EXCEPTHOOKS_INSTALLED = False


def _install_excepthooks() -> None:
    """Route uncaught exceptions (main thread AND worker threads) through the logger so the rotating
    file captures the full traceback instead of it vanishing to an unread stderr. Idempotent."""
    global _EXCEPTHOOKS_INSTALLED
    if _EXCEPTHOOKS_INSTALLED:
        return
    prev = sys.excepthook

    def _hook(exc_type, exc, tb):
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            logger.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        prev(exc_type, exc, tb)

    sys.excepthook = _hook
    if hasattr(threading, "excepthook"):
        def _thook(args):
            logger.critical("uncaught exception in thread %s",
                            getattr(args, "thread", None),
                            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.excepthook = _thook
    _EXCEPTHOOKS_INSTALLED = True


def configure_logging(data_dir=None, *, level=logging.INFO) -> Path | None:
    """Attach a rotating file handler to the root logger (capturing ``kg_engine`` at INFO and the
    ``mcp`` library at WARNING) writing to ``<data_dir>/server.log``, and install the uncaught-exception
    hooks. Best-effort: a logging-setup failure must never stop the server from coming up, so any error
    is swallowed and None returned. Idempotent — a prior kg-server handler is replaced, so repeated calls
    (e.g. in tests) don't accumulate handlers or duplicate lines. Returns the log path on success."""
    try:
        path = server_log_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_kg_server_log", False):
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:  # noqa: BLE001 — closing a stale handler must never raise here
                    pass
        handler = RotatingFileHandler(path, maxBytes=SERVER_LOG_MAX_BYTES,
                                      backupCount=SERVER_LOG_BACKUP_COUNT, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [pid %(process)d]: %(message)s"))
        handler.setLevel(level)
        handler._kg_server_log = True  # type: ignore[attr-defined]  # marker for idempotent replace
        root.addHandler(handler)
        # Records propagate to the root handler; raise each logger's own level so the records are emitted
        # (the root level only gates records logged directly on root, not propagated ones).
        logging.getLogger("kg_engine").setLevel(level)
        logging.getLogger("mcp").setLevel(logging.WARNING)
        _install_excepthooks()
        return path
    except Exception:  # noqa: BLE001 — logging setup is best-effort; never block server startup on it
        return None


class _Watchdog:
    """A daemon thread that force-exits the process if a single MCP handler runs longer than `timeout`.

    FastMCP runs sync tools directly on the event loop (no thread offload), so a wedged handler — a
    deadlocked write, a runaway projection — blocks the whole loop and the client just sees "Running…"
    forever with no recovery. This observer thread (it never touches the handler, only watches a
    monotonic start stamp the tool envelope updates) breaks that: on timeout it dumps every thread's
    stack to the log and exits, so the Node supervisor relaunches a FRESH process. Crash-safe canon I/O
    (atomic temp+replace, the reclaimable lease) makes a hard exit recoverable; idempotent write receipts
    make a lost in-flight response harmless to retry.

    `on_trip` is injected so tests can assert a trip WITHOUT killing the test process (default os._exit)."""

    def __init__(self, timeout: float, *, on_trip=None, poll: float | None = None):
        self.timeout = float(timeout)
        self._poll = poll if poll is not None else max(1.0, self.timeout / 10.0)
        self._on_trip = on_trip or (lambda: os._exit(EXIT_WATCHDOG))
        self._lock = threading.Lock()
        self._name: str | None = None
        self._started: float = 0.0
        self._depth = 0
        # A multi-file canon write (kg_rename/kg_merge/kg_write) marks a CRITICAL section: a force-exit
        # mid-batch would leave the mutation half-applied (rename/merge are not crash-atomic across files).
        # When a handler overruns while `_critical` is set, the watchdog grants ONE grace extension before
        # tripping, so a slow (e.g. network-vault) atomic batch isn't killed mid-write (review:
        # watchdog-force-exit-mid-multi-file-write). Bounded to a single extension per handler span.
        self._critical = 0
        self._extended = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def enter(self, name: str) -> None:
        with self._lock:
            self._depth += 1
            if self._depth == 1:
                self._name, self._started = name, time.monotonic()
                self._extended = False

    def begin_critical(self) -> None:
        with self._lock:
            self._critical += 1

    def end_critical(self) -> None:
        with self._lock:
            self._critical = max(0, self._critical - 1)

    def exit(self) -> None:
        with self._lock:
            self._depth = max(0, self._depth - 1)
            if self._depth == 0:
                self._name, self._started = None, 0.0

    def overdue(self, now: float | None = None) -> "tuple[str, float] | None":
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._depth > 0 and self._name is not None:
                elapsed = now - self._started
                if elapsed > self.timeout:
                    # A multi-file canon write in flight: grant ONE grace extension (reset the clock) so a
                    # slow atomic batch isn't force-killed mid-write, leaving a half-applied rename/merge.
                    if self._critical > 0 and not self._extended:
                        self._extended = True
                        self._started = now
                        return None
                    return self._name, elapsed
        return None

    def start(self) -> "_Watchdog":
        if self.timeout <= 0 or self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="kg-watchdog", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            hit = self.overdue()
            if hit:
                name, elapsed = hit
                self._trip(name, elapsed)
                return

    def _trip(self, name: str, elapsed: float) -> None:
        stacks = []
        for tid, frame in sys._current_frames().items():
            stacks.append(f"--- thread {tid} ---\n" + "".join(traceback.format_stack(frame)))
        logger.critical("watchdog: handler %r exceeded %.0fs (ran %.0fs); forcing a fresh process so "
                        "the supervisor relaunches.\n%s", name, self.timeout, elapsed, "\n".join(stacks))
        # FLUSH (don't logging.shutdown()) before the trip: the default on_trip is os._exit, which bypasses
        # the interpreter's atexit flush, so the critical record must be pushed to disk now. shutdown()
        # would CLOSE every handler process-wide — harmful to a still-running process and to the test suite.
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:  # noqa: BLE001 — a flush hiccup must not stop the trip
                pass
        self._on_trip()


# The active watchdog, set by main(). The tool envelope (_tool_result) feeds it without changing any
# wrapper signature (so the manifest scrape and FastMCP schema are untouched); None disables feeding.
_WATCHDOG: "_Watchdog | None" = None

# The active engine, set by _register(). The module-scope tool envelope has no engine reference, so it
# reads the CONFIGURED sensitivity from here to scrub raised-exception messages at the operator's chosen
# tier (not a hardcoded 'medium') before they cross the §1.9 egress — mirroring the engine's own
# _scrub_error path (review: error-envelope-ignores-configured-sensitivity). None → default 'medium'.
_ACTIVE_ENGINE: "KGEngine | None" = None


def _active_sensitivity() -> str:
    eng = _ACTIVE_ENGINE
    return eng.sensitivity if eng is not None else "medium"


class _SourceResolver:
    """Resolves the configured source path to a SourceSet, memoized on the aggregate
    (resolved-file-list, mtime) signature so an added/removed/edited file is picked up while the
    resolve+read stays off the hot path. Held by KGEngine (kg_scrub/kg_write span verification + the
    projector wiring) and constructed afresh for the read-only PreToolUse-hook projector, so both read
    the IDENTICAL source bytes. A single configured file is a one-entry SourceSet, byte-identical to the
    prior single-blob path."""

    def __init__(self, source_path=None):
        self.source_path = Path(source_path) if source_path else None
        self._cache: "tuple[tuple, SourceSet] | None" = None  # (signature, SourceSet) memo

    def set(self) -> SourceSet:
        sig = SourceSet.signature(self.source_path)
        if self._cache is None or self._cache[0] != sig:
            self._cache = (sig, SourceSet(self.source_path))
        return self._cache[1]

    def text(self) -> str:
        return self.set().concat

    def set_path(self, source_path) -> None:
        """Re-point at a new source (and drop the memo) IN PLACE, so a holder of .set/.text — e.g. the
        already-wired projector — sees the change without being reconstructed."""
        self.source_path = Path(source_path) if source_path else None
        self._cache = None


def _wire_projector(canon, derived_dir, *, sources, pack, metrics_mode) -> Projector:
    """The SINGLE construction site for a Projector's source-corpus + specificity-seed + metrics wiring,
    shared by the writer engine (KGEngine.__init__) and the read-only PreToolUse hook
    (KGEngine.read_only_projector). Routing both through here means a hook-triggered projection computes
    the SAME IDF/specificity gate, spec_betweenness, and R3 stale-verdict scan as the server — it can
    never write a degraded empty-corpus derived layer the server then serves as fresh (finding:
    precontext-bypasses-facade). `sources` is a _SourceResolver; `pack` may be None."""
    return Projector(canon, derived_dir, metrics_mode=metrics_mode,
                     source_text=sources.text, source_set=sources.set,
                     specificity_seeds=lambda: dict(getattr(pack, "specificity_seeds", {}) or {}))


class KGEngine:
    """Stateful facade over canon + boundary + projector + reconciler + scrubber."""

    def __init__(self, project_dir, data_dir=None, *, source_path=None, pack_path=None,
                 sensitivity="medium", metrics_mode="structure_only",
                 max_edges_per_kb=DEFAULT_MAX_EDGES_PER_KB):
        self.project_dir = Path(project_dir)
        self.data_dir = Path(data_dir) if data_dir else (self.project_dir / ".kg-data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.canon = Canon(self.project_dir)
        self.reconciler = Reconciler(self.canon)
        # §1.8 grounding-audit log — the forge-detection WRITER half (the reconciler is the reader). A
        # collaborator, not inline, so the crash-safe append/truncate protocol is unit-testable.
        self._audit_log = GroundAuditLog(self.canon.root / GROUND_AUDIT)
        # The configured source: a single file (back-compat), or a DIRECTORY / GLOB of .md/.txt (R4).
        # Resolution + memo live in _SourceResolver, shared verbatim with the read-only PreToolUse hook.
        self._sources = _SourceResolver(source_path)
        # Load the pack BEFORE constructing the projector so its specificity_seeds are wired through the
        # SAME _wire_projector seam the read-only hook uses — no construction-site drift between the two
        # (finding: precontext-bypasses-facade).
        self.pack = None
        if pack_path and Path(pack_path).exists():
            try:
                self.pack = load_pack(pack_path)
            except Exception:  # noqa: BLE001 — a bad pack must not crash the server
                self.pack = None
        # The projector reads source/specificity lazily, once per real reprojection, off the hot path.
        self.projector = _wire_projector(self.canon, self.data_dir / "derived",
                                         sources=self._sources, pack=self.pack, metrics_mode=metrics_mode)
        self.scrubber = Scrubber(sensitivity)
        self._scrub_map: dict[str, str] = {}  # accumulated egress placeholder -> original (§1.9)
        self.sensitivity = sensitivity
        self.metrics_mode = metrics_mode
        self.max_edges_per_kb = max_edges_per_kb
        # Reason string the last reprojection failed with (None when projection is healthy). Reads serve
        # the existing/empty derived layer with this flag set rather than raising (defense: a projection
        # hiccup degrades a read, it never crashes a tool — see _ensure_projected).
        self._projection_degraded: str | None = None
        # Idempotency: an in-memory LRU of {idempotency_key → kg_write response} so re-sending an
        # identical write (after a lost transport response) is a TRUE no-op that returns the SAME receipt
        # and counts, not a second pass. Bounded by _WRITE_CACHE_MAX; lost on restart, but the
        # payload-derived `receipt` + id-dedup keep a post-restart retry safe regardless (§1.4).
        self._write_cache: "OrderedDict[str, dict]" = OrderedDict()

    # ---- source set (for span verification) — delegate to the shared resolver
    def source_set(self) -> SourceSet:
        """The resolved {basename → text} view over the configured source(s) (R4), memoized off the hot
        path. A single configured file is a one-entry SourceSet, byte-identical to the prior path."""
        return self._sources.set()

    def source_text(self) -> str:
        """The configured source(s) concatenated — feeds the flood-budget size and the projector's IDF
        corpus. Span verification itself is per-file via source_set().verifies, not this blob."""
        return self._sources.text()

    @property
    def source_path(self):
        """The configured source Path (or None), backed by the shared resolver. Kept as a settable
        attribute for back-compat: the headless backend reads `.name`, and a test re-points it — the
        setter mutates the resolver in place so the already-wired projector follows."""
        return self._sources.source_path

    @source_path.setter
    def source_path(self, value) -> None:
        self._sources.set_path(value)

    @classmethod
    def read_only_projector(cls, project_dir, data_dir, *, source_path=None, pack_path=None,
                            metrics_mode="structure_only") -> Projector:
        """A Projector wired IDENTICALLY to a live engine's (same source corpus + specificity seeds +
        metrics_mode) but over a no-side-effect Canon(ensure_layout=False) — for the read-only PreToolUse
        hook. Goes through the SAME _wire_projector seam as __init__, so the hook can never project a
        degraded (empty-corpus) derived layer the server then serves as fresh (finding:
        precontext-bypasses-facade). The pack is loaded eagerly (the hook holds no engine); a missing or
        bad pack degrades to no specificity seeds, exactly as __init__ does."""
        canon = Canon(project_dir, ensure_layout=False)
        pack = None
        if pack_path and Path(pack_path).exists():
            try:
                pack = load_pack(pack_path)
            except Exception:  # noqa: BLE001 — a bad pack must not break the read hook
                pack = None
        return _wire_projector(canon, Path(data_dir) / "derived",
                               sources=_SourceResolver(source_path), pack=pack, metrics_mode=metrics_mode)

    # ---- tools -----------------------------------------------------------
    def kg_ping(self) -> dict:
        return {"name": "creativity-graph", "version": __version__,
                "metrics_mode": self.metrics_mode, "sensitivity": self.sensitivity,
                "pack_loaded": self.pack is not None}

    def kg_scrub(self, text: str | None = None) -> dict:
        """Egress scrub (§1.9): redact secrets (always) + PII (per sensitivity) with CONSISTENT
        placeholders before any text is handed to a subagent for semantic work. Accumulates the local
        placeholder->original mapping so kg_write can restore spans to the original for the canon (the
        scrub protects the egress, not the local canon). Pass `text` to scrub a snippet, or omit to scrub
        the configured source. Returns the scrubbed text the subagent should see."""
        src = text if text is not None else self.source_text()
        scrubbed, mapping = self.scrubber.scrub(src)
        self._scrub_map.update(mapping)
        return {"scrubbed": scrubbed, "redactions": len(mapping),
                "sensitivity": self.sensitivity, "categories": sorted({k.split(":")[0].strip("⟦") for k in mapping})}

    def _restore_fn(self):
        """The §1.9 span-restore: map placeholder spans back to the original before span verification,
        but ONLY when a scrub happened this session (else None — verify the span as written)."""
        return (lambda s: Scrubber.restore(s, self._scrub_map)) if self._scrub_map else None

    def _scrub_error(self, msg) -> str:
        """Scrub a handler's error string (a `{e}` exception interpolation — a corrupt-node parse error
        quoting un-scrubbed canon content, an OSError carrying a vault path) to the SAME §1.9 egress
        standard `kg_scrub` applies, before it is returned to the session. Uses the engine's configured
        sensitivity; never raises (see `_scrub_error_text`)."""
        return _scrub_error_text(msg, sensitivity=self.sensitivity)

    @contextlib.contextmanager
    def _critical_write(self):
        """Mark a multi-file canon-mutation region as CRITICAL for the watchdog: while it is held, a
        handler that overruns the watchdog timeout is granted ONE grace extension before the force-exit,
        so a slow atomic batch (network vault, contended fsync) isn't killed mid-write leaving a
        half-applied rename/merge (review: watchdog-force-exit-mid-multi-file-write). Best-effort — a
        missing/disabled watchdog is a no-op."""
        wd = _WATCHDOG
        if wd is not None:
            wd.begin_critical()
        try:
            yield
        finally:
            if wd is not None:
                wd.end_critical()

    @staticmethod
    def _append_note(existing: str, addition: str) -> str:
        """Append `addition` to a notes field with the load-bearing ` | ` separator (the field is later
        parsed/displayed); names the separator once."""
        return (existing + " | " if existing else "") + addition

    @staticmethod
    def _payload_receipt(payload: dict) -> str:
        """A deterministic receipt token for a write payload: a short hash over the SORTED set of
        canonical ids the payload targets AND their content-bearing fields (node label/body/type/axes;
        edge span/notes/confidence/axes). Same payload → same receipt, independent of dedup status or
        process restarts — so a lost transport response is harmless: re-sending the identical payload
        yields the identical `receipt`, and "did my write land?" becomes a cheap retry rather than an
        out-of-band read of the canon dir. Folding the content in (not just the ids) is what lets the
        idempotency replay branch tell a genuine retry (identical content) apart from a same-ids payload
        whose text CHANGED (e.g. a corrected span) — the latter must be processed, not silently replayed
        (review: receipt-id-only-drops-content-correction)."""
        from .model import edge_id as _edge_id
        items = []
        for n in (payload or {}).get("nodes") or []:
            n = n or {}
            nid = n.get("id") or n.get("label") or ""
            content = {k: n.get(k) for k in
                       ("label", "body", "node_type", "provenance", "authored_by",
                        "epistemic_state", "confidence") if k in n}
            items.append("node:" + slug(str(nid)) + "|"
                         + json.dumps(content, sort_keys=True, ensure_ascii=True, default=str))
        for e in (payload or {}).get("edges") or []:
            e = e or {}
            eid = _edge_id(str(e.get("source", "")), str(e.get("relation", "")),
                           str(e.get("target", "")))
            content = {k: e.get(k) for k in
                       ("span", "notes", "note", "confidence", "confidence_score", "provenance",
                        "authored_by", "epistemic_state", "source_file") if k in e}
            items.append("edge:" + eid + "|"
                         + json.dumps(content, sort_keys=True, ensure_ascii=True, default=str))
        digest = hashlib.sha1("\n".join(sorted(items)).encode("utf-8")).hexdigest()
        return f"rcpt_{digest[:16]}"

    def kg_write(self, payload: dict, *, message: str = "kg_write", existing_nodes=None,
                 idempotency_key: str | None = None) -> dict:
        """Validate an extraction payload at the boundary and write accepted/demoted items.

        `existing_nodes` is the canon baseline used for dedup + rate-limit seeding; it defaults to a
        fresh parse (every existing call site is unchanged). The headless backend threads an
        incrementally-maintained baseline so it doesn't re-parse the entire canon once per section
        (backend-1/server-16).

        **Idempotency (a lost response is harmless).** The response always carries a deterministic
        `receipt` derived from the payload (`_payload_receipt`). If `idempotency_key` is supplied and was
        seen before in this process WITH THE SAME PAYLOAD (same receipt), the cached response is returned
        VERBATIM with `idempotent_replay: True` — no re-validation, no second write — so a client that
        retries after a dropped transport result gets the IDENTICAL receipt + dispositions instead of a
        confusing all-deduped second pass. If the same key is reused with a DIFFERENT payload (a caller
        contract violation), the new write is NOT silently dropped: it is processed normally and re-caches
        the key (a warning is logged). Validation is never weakened: a mismatching/first-seen key validates
        and writes normally. Idempotency is also intrinsic without a key — kg_write dedups by canonical id,
        so a re-send creates no duplicates regardless (§1.4)."""
        receipt = self._payload_receipt(payload)
        if idempotency_key:
            cached = self._write_cache.get(idempotency_key)
            if cached is not None:
                if cached.get("receipt") == receipt:
                    self._write_cache.move_to_end(idempotency_key)
                    # Deep-copy so the replayed receipt's nested `dispositions`/`details`/`written_nodes`
                    # do NOT alias the cached objects — a caller mutating a replay can't corrupt the cache.
                    return {**copy.deepcopy(cached), "idempotent_replay": True}
                # same key, DIFFERENT payload: a caller error. Don't replay a stale receipt and silently
                # drop this write — process it normally (it re-caches the key below).
                logger.warning("idempotency_key %r reused with a different payload; processing the new "
                               "write instead of replaying the cached receipt", idempotency_key)
        # if egress scrubbing happened this session, restore placeholder spans to the original before
        # span verification, and store the original in the canon (§1.9).
        restore = self._restore_fn()
        if existing_nodes is None:
            existing_nodes = self.canon.all_nodes()  # read once; derive edges + node baseline from it
        existing_edges = [e for n in existing_nodes for e in n.edges]
        results = validate_payload(payload, pack=self.pack, source_text=self.source_text(),
                                   sources=self.source_set(),
                                   existing=existing_edges,
                                   existing_node_ids={n.id for n in existing_nodes},
                                   restore=restore, max_edges_per_kb=self.max_edges_per_kb)
        nodes = merge_results_into_nodes(results)
        with self._critical_write():
            info = self.canon.write_nodes(list(nodes.values()), message=message) if nodes else None
        rolled_back = bool(info and info.rolled_back)
        summary: dict = {d.value: 0 for d in Disposition}
        for r in results:
            summary[r.disposition.value] += 1
        # CONTRACT (F10/M4): the dispositions summary and written_nodes are built from PRE-write
        # ValidationResults; if the batch ROLLED BACK nothing persisted. Re-bucket the would-have-been
        # ACCEPTED/DEMOTED counts into a `rolled_back` bucket and empty written_nodes so the payload can
        # never contradict `rolled_back: True`. Backend consumers: when rolled_back is True,
        # written_nodes is [] and the accepted/demoted counts must NOT be trusted/accumulated.
        written = list(nodes)
        if rolled_back:
            persisted = (Disposition.ACCEPTED.value, Disposition.DEMOTED.value)
            summary["rolled_back"] = sum(summary.get(d, 0) for d in persisted)
            for d in persisted:
                summary[d] = 0
            written = []
        out = {
            "dispositions": summary,
            "details": [{"kind": r.kind, "id": getattr(r.item, "id", None), "disposition": r.disposition.value,
                         "reason": r.reason, "retryable": r.retryable} for r in results],
            "written_nodes": written,
            "rolled_back": rolled_back,
            "error": (info.error if rolled_back else None),
            "receipt": receipt,
        }
        # Cache the response under the idempotency key (bounded LRU) so an exact retry replays it verbatim.
        # Do NOT cache a rolled-back batch: a rollback is a transient failure (e.g. an I/O error), and a
        # retry should be allowed to actually write, not replay the failure.
        if idempotency_key and not rolled_back:
            # Snapshot a deep copy so the returned `out` (which the caller may mutate) and the cached
            # receipt never share nested structures.
            self._write_cache[idempotency_key] = copy.deepcopy(out)
            self._write_cache.move_to_end(idempotency_key)
            while len(self._write_cache) > _WRITE_CACHE_MAX:
                self._write_cache.popitem(last=False)
        return out

    def kg_propose(self, payload: dict, *, message: str = "kg_propose") -> dict:
        """Write hypothesized candidates through the boundary (PLAN Stage 1: the propose lane).

        A thin, explicit alias over `kg_write` that keeps the two write lanes legible at the call site:
        every item is forced to `provenance=hypothesized`, and any item that arrives explicitly claiming
        a text-claim provenance (`span-present`/`inferred`) is REFUSED with reason `propose-lane-text-claim`
        rather than silently re-lanned — text claims belong on `kg_write`, proposals belong here. The
        accepted items then transit the SAME boundary (`validate_payload`), so the hypothesized-lane rules
        (no span required, forged verdicts demoted, failure-collapse quarantined, pack vocabulary enforced)
        apply uniformly."""
        payload = dict(payload or {})
        refused: list[dict] = []

        def _lane(items, kind):
            kept = []
            for it in (items or []):
                it = dict(it or {})
                prov = it.get("provenance")
                if prov in (Provenance.SPAN_PRESENT.value, Provenance.INFERRED.value):
                    refused.append({"kind": kind,
                                    "id": it.get("id") or it.get("source") or it.get("label"),
                                    "disposition": Disposition.REJECTED.value,
                                    "reason": "propose-lane-text-claim", "retryable": False})
                else:
                    it["provenance"] = Provenance.HYPOTHESIZED.value  # force the lane
                    kept.append(it)
            return kept

        clean = {"nodes": _lane(payload.get("nodes"), "node"),
                 "edges": _lane(payload.get("edges"), "edge")}
        if "complete" in payload:
            clean["complete"] = payload["complete"]
        out = self.kg_write(clean, message=message)
        # fold the call-site refusals into the same response shape kg_write returns
        out["details"] = refused + out["details"]
        out["dispositions"][Disposition.REJECTED.value] = (
            out["dispositions"].get(Disposition.REJECTED.value, 0) + len(refused))
        out["propose_lane"] = True
        out["refused_text_claims"] = len(refused)
        return out

    def kg_ground(self, target_id: str, verdict: str, *, by: str = "agent", kind: str = "edge",
                  note: str = "", support_span: str = "", support_note: str = "") -> dict:
        """Apply a grounding verdict (the ONLY path that may set a verdict state). Stamps the verdict
        and appends an audit record so the reconciler treats the transition as legitimate (§1.8).

        **Promotion of a hypothesis requires support (PLAN Stage 8 / §1.2-3).** A `hypothesized` edge
        may become `grounded` ONLY when a grounder supplies support, which UPGRADES its provenance:
        `support_span` (a verbatim substring of the source) → `span-present`; `support_note` (an external
        citation, no span) → `inferred`. Without either, grounding a hypothesis to `grounded` is refused
        with `hypothesis-needs-support` — generated ideas become grounded knowledge only by earning it.
        The same gate applies to a hypothesized NODE (a compression node / primitive from the propose
        lane): it too earns grounding only with support, restated into the node body (a Node has no span
        field). `support_*` are ignored for non-hypothesized items and for any verdict other than `grounded`.

        `note` is appended to the EDGE's `notes` and is **edge-only**: a Node has no notes field, so a
        `note` passed with `kind='node'` is ignored (the verdict's audit record still captures `by`)."""
        # Strip/normalize inputs like kg_rename/kg_merge do, so a stray-whitespace verdict (" grounded ")
        # isn't mis-classified as invalid and a stray-whitespace `kind` is canonicalized before dispatch.
        verdict = verdict.strip().lower()
        if verdict not in VALID_VERDICTS:
            return {"ok": False, "error": f"invalid verdict {verdict!r}"}
        # `kind` selects the dispatch branch; reject anything outside {node,edge} up front (mirroring the
        # verdict clamp) so a typo'd `kind` (e.g. 'Node', 'edges', '') can't fall through the else into the
        # edge path and surface a misleading 'edge not found' for what was meant as a node verdict.
        kind = kind.strip()
        if kind not in ("node", "edge"):
            return {"ok": False, "error": f"invalid kind {kind!r}; expected node|edge"}
        # `by` is provenance, not a free-text field: clamp to the known actors so a stray value can't
        # masquerade as a verdict author (the MCP tool surface already pins this to "agent").
        by = by if by in VALID_ACTORS else "agent"
        state = EpistemicState(verdict)
        promoted_to = None
        # Acquire the single-writer lease FIRST, then read the owning node FRESH under the lease, mutate,
        # audit, and write — so the whole read-modify-write is atomic w.r.t. other writers. Reading before
        # locking (the old order) let a concurrent multi-process grounding clobber our edits with a
        # whole-node overwrite (lost update, F17/L5). The lease also still guards the audit-append +
        # write + compensating-truncate sequence (server-3); write_one re-acquires it re-entrantly.
        # Use the bounded-BLOCKING acquire (as kg_write's write_nodes does), not the non-blocking
        # try_acquire_lock: a verdict/rename/merge is a WRITER, so brief cross-process contention (the
        # detached reconcile worker or a headless backend holding the lease for one note) must SERIALIZE
        # cleanly instead of failing outright with a spurious locked-vault error. The 30s budget stays
        # well under the watchdog timeout (review: writers-use-nonblocking-lock).
        try:
            self.canon._acquire_lock()
        except RuntimeError:
            return {"ok": False, "error": "canon vault is locked by another live session"}
        try:
            if kind == "node":
                # Canonicalize the node id like kg_rename/kg_merge (`slug`) so a non-canonical id doesn't
                # yield a false "node not found". Edge ids already arrive canonical from reads, so the edge
                # branch below deliberately does NOT slug `target_id`.
                target_id = slug(target_id)
                if not self.canon.exists(target_id):
                    return {"ok": False, "error": "node not found"}
                try:
                    node = self.canon.read_node(target_id)  # corrupt/invalid-UTF-8 note → structured error (F13/L1)
                except Exception as e:  # noqa: BLE001 — surface as a structured error, not an MCP exception
                    return {"ok": False, "error": self._scrub_error(f"node unreadable: {e}")}
                # the hypothesized→grounded promotion gate applies to NODES too: kg_operate writes
                # hypothesized compression nodes/primitives via the propose lane, so a generated node must
                # earn grounding with support, not become grounded knowledge for free (mirrors the edge
                # gate; decided BEFORE any state change so a refusal leaves the node untouched).
                if node.provenance == Provenance.HYPOTHESIZED and state == EpistemicState.GROUNDED:
                    promoted_to, err = self._promote_hypothesis_node(node, support_span, support_note)
                    if err:
                        return {"ok": False, "error": err}
                frm = node.epistemic_state.value
                node.epistemic_state = state
                key = f"node:{node.id}"
            else:
                node = self._owner_of_edge(target_id)
                if node is None:
                    return {"ok": False, "error": "edge not found"}
                edge = next(e for e in node.edges if e.id == target_id)
                # the hypothesized→grounded promotion gate: a span-less proposal earns grounding only with
                # support, which upgrades its provenance. Decided BEFORE any state change so a refusal leaves
                # the edge untouched (no audit record, no write).
                if edge.provenance == Provenance.HYPOTHESIZED and state == EpistemicState.GROUNDED:
                    promoted_to, err = self._promote_hypothesis(edge, support_span, support_note)
                    if err:
                        return {"ok": False, "error": err}
                frm = edge.epistemic_state.value
                edge.epistemic_state = state
                edge.verdict_by = by
                edge.verdict_at = utcnow()
                if note:
                    edge.notes = self._append_note(edge.notes, note)
                key = edge.id
            # Append the audit record BEFORE persisting the verdict (a CRASH between the two leaves an
            # audit record with no state change — harmless, unconsumed — rather than a verdict with no
            # audit record, which the reconciler would re-quarantine), and truncate it back on a caught
            # write failure so an orphan record can't inflate _forged's count (server-3). The crash-safe
            # offset/truncate dance lives in GroundAuditLog.audited_write, shared with kg_rename.
            err_holder: dict = {}

            def _attempt():
                try:
                    self.canon.write_one(node)
                    return True, None
                except Exception as e:  # noqa: BLE001 — surface as a structured error, not an MCP exception
                    err_holder["error"] = self._scrub_error(f"write failed: {e}")
                    return False, None

            self._audit_log.audited_write([(key, frm, verdict, by)], _attempt)
            if err_holder:  # the transition never happened; its record was truncated
                return {"ok": False, "error": err_holder["error"]}
            out = {"ok": True, "key": key, "from": frm, "to": verdict, "by": by}
            if promoted_to:  # a hypothesis was promoted — its provenance was upgraded (PLAN Stage 8)
                out["provenance_upgraded_to"] = promoted_to
            return out
        finally:
            self.canon._release_lock()

    def _promote_hypothesis(self, edge, support_span: str, support_note: str):
        """The §1.2-3 / PLAN-Stage-8 hypothesized→grounded promotion gate: a span-less proposal earns
        grounding only with support, which UPGRADES its provenance. Mutates `edge` in place on success
        and returns (promoted_to, None); on a refusal it leaves the edge UNTOUCHED and returns
        (None, error) so the caller can refuse before any state change / audit / write. `support_span`
        (a verbatim source substring) → span-present; `support_note` (an external citation) → inferred;
        neither → `hypothesis-needs-support`."""
        restore = self._restore_fn()
        if support_span and support_span.strip():
            check = restore(support_span) if restore else support_span
            # source-aware (R4): verify against the edge's named source if it has one, else
            # any declared source. The not-in-ANY-source contract is unchanged
            # (support-span-not-in-source) — a promotion span just has to exist SOMEWHERE.
            if not self.source_set().verifies(check, source_file=edge.source_file):
                return None, "support-span-not-in-source"
            if len(normalize_text(check).replace(" ", "")) < MIN_SPAN_CHARS:
                return None, "support-span-too-short"
            edge.span = check
            edge.provenance = Provenance.SPAN_PRESENT       # upgraded: now citable
            return Provenance.SPAN_PRESENT.value, None
        if support_note and support_note.strip():
            edge.provenance = Provenance.INFERRED            # upgraded: asserted via external citation
            edge.notes = self._append_note(edge.notes, f"citation: {support_note.strip()}")
            return Provenance.INFERRED.value, None
        return None, "hypothesis-needs-support"

    def _promote_hypothesis_node(self, node, support_span: str, support_note: str):
        """The node counterpart of `_promote_hypothesis`: a hypothesized NODE (a generated compression
        node / primitive from the propose lane) earns grounding only with support, which UPGRADES its
        provenance. A Node has no `span`/`notes` field, so the support is restated into the node BODY (the
        only persisted free-text — ARCHITECTURE: "Body prose … may restate cited spans") rather than a
        stray span attr. Mutates `node` in place on success and returns (promoted_to, None); on a refusal
        it leaves the node UNTOUCHED and returns (None, error). `support_span` (a verbatim source
        substring) → span-present; `support_note` (an external citation) → inferred; neither →
        `hypothesis-needs-support`."""
        restore = self._restore_fn()
        if support_span and support_span.strip():
            check = restore(support_span) if restore else support_span
            if not self.source_set().verifies(check):
                return None, "support-span-not-in-source"
            if len(normalize_text(check).replace(" ", "")) < MIN_SPAN_CHARS:
                return None, "support-span-too-short"
            node.body = self._append_body(node.body, f"grounding span: {check}")
            node.provenance = Provenance.SPAN_PRESENT       # upgraded: now citable
            return Provenance.SPAN_PRESENT.value, None
        if support_note and support_note.strip():
            node.body = self._append_body(node.body, f"citation: {support_note.strip()}")
            node.provenance = Provenance.INFERRED            # upgraded: asserted via external citation
            return Provenance.INFERRED.value, None
        return None, "hypothesis-needs-support"

    @staticmethod
    def _append_body(existing: str, addition: str) -> str:
        """Append `addition` as its own paragraph to a node body, preserving any existing prose."""
        existing = (existing or "").rstrip("\n")
        return (existing + "\n\n" if existing else "") + addition

    def _owner_of_edge(self, edge_id: str) -> Node | None:
        # O(1) lookup via the derived index (id -> source) instead of an O(N) full-canon scan per
        # kg_ground call, which made draining the grounding queue quadratic (server-2). The index is
        # read-only here; on a miss (just-written edge not yet projected, or no index) fall back to a
        # scan so correctness never depends on derived freshness.
        #
        # Do NOT _ensure_projected() here (review-M3): every prior kg_ground bumps node.updated_at, so
        # is_stale() returns True on the next call and _ensure_projected would run a full
        # betweenness/gate reproject — making a /kg-ground drain O(N * V*E), exactly the quadratic the
        # index was added to remove. Correctness doesn't need freshness: a just-written edge the index
        # hasn't seen is found by the canon-scan fallback below.
        try:
            src = self.projector.owner_of_edge(edge_id)
            if src and self.canon.exists(src):
                node = self.canon.read_node(src)
                if any(e.id == edge_id for e in node.edges):
                    return node
        except Exception as e:  # noqa: BLE001 — index trouble must never break grounding; fall back
            logger.debug("edge-owner index lookup failed (%s); falling back to full canon scan", e)
        for n in self.canon.all_nodes():
            if any(e.id == edge_id for e in n.edges):
                return n
        return None

    def _audit_path(self) -> Path:
        """The grounding-audit log path. Thin accessor (the durability protocol lives in GroundAuditLog);
        kept because tests read the raw audit bytes through it."""
        return self._audit_log.path

    def _rewrite_endpoints(self, edge, old: str, new: str):
        """Rewrite an edge's old→new endpoints, recompute its deterministic id from the new endpoints,
        and report the integration-1 migration. Returns (changed, migration | None) where migration =
        (new_id, state_value) iff the id actually CHANGED and the edge is in a policed (verdict-or-
        obsolete) state — the load-bearing record that preserves grounding/failure memory across a
        rename, kept in ONE place so the two rename loops can never drift apart."""
        from .model import edge_id
        old_eid = edge.id
        if edge.source == old:
            edge.source = new
        if edge.target == old:
            edge.target = new
        edge.id = edge_id(edge.source, edge.relation, edge.target)  # keep id consistent with endpoints
        changed = edge.id != old_eid
        migration = ((edge.id, edge.epistemic_state.value)
                     if changed and edge.epistemic_state in GROUNDABLE_STATES else None)
        return changed, migration

    def kg_rename(self, old_id: str, new_id: str, *, message: str = "kg_rename") -> dict:
        """Rename a node and rewrite every edge endpoint referencing it (single-canonical-edge safe)."""
        old, new = slug(old_id), slug(new_id)
        # Acquire the single-writer lease FIRST, then read the canon FRESH under the lease and compute
        # the migration set + touched notes, all before write_nodes. Reading BEFORE locking (the old
        # order) let a concurrent cross-process kg_ground stamp a verdict on a SIBLING edge of a touched
        # node in the gap; this rename then wrote its stale in-memory copy verbatim (merge=False) and
        # silently clobbered that just-stamped verdict — a lost update of grounding memory the reconciler
        # can't recover (it only re-quarantines forgeries, never resurrects a lost legitimate verdict).
        # Same fix as kg_ground (F17/L5); the lease also stays held across the whole audit + write +
        # unlink + commit sequence so the migrating records and their compensating truncate are atomic
        # w.r.t. other writers (server-3). write_nodes/_acquire_lock are re-entrant, so the inner write
        # still works.
        # bounded-BLOCKING acquire (mirrors kg_write) so brief cross-process contention serializes rather
        # than failing this writer outright (review: writers-use-nonblocking-lock).
        try:
            self.canon._acquire_lock()
        except RuntimeError:
            return {"ok": False, "error": "canon vault is locked by another live session",
                    "old": old, "new": new}
        try:
            if not self.canon.exists(old):
                return {"ok": False, "error": "node not found"}
            if self.canon.exists(new):
                return {"ok": False, "error": "target id exists"}
            try:
                node = self.canon.read_node(old)  # corrupt/invalid-UTF-8 note → structured error (F13/L1)
            except Exception as e:  # noqa: BLE001 — surface as a structured error, not an MCP exception
                return {"ok": False, "error": self._scrub_error(f"node unreadable: {e}"),
                        "old": old, "new": new}
            # A rename recomputes edge ids (and the node id), but the kg_ground audit record + reconciler
            # baseline are keyed by those ids. Collect every policed-state (verdict OR obsolete) item whose
            # id CHANGES so we can write a migrating audit record for the NEW id — otherwise the reconciler
            # sees a verdict at an id with no audit record and re-quarantines it, silently erasing the
            # grounding/failure memory (integration-1).
            migrations: list[tuple[str, str]] = []  # (new_key, state_value)
            if node.epistemic_state in GROUNDABLE_STATES:
                migrations.append((f"node:{new}", node.epistemic_state.value))
            node.id = new
            for e in node.edges:
                _, mig = self._rewrite_endpoints(e, old, new)
                if mig:
                    migrations.append(mig)
            touched = [node]
            for other in self.canon.all_nodes():
                if other.id == old:
                    continue
                node_changed = False
                for e in other.edges:
                    changed, mig = self._rewrite_endpoints(e, old, new)
                    node_changed |= changed
                    if mig:
                        migrations.append(mig)
                if node_changed:
                    touched.append(other)
            # Emit the migrating audit records (compensated by truncation if the batch rolls back, like
            # kg_ground), then write the corrected nodes VERBATIM (merge=False): merging would
            # re-introduce each note's pre-rename edges (different id -> not deduped) and leave dangling
            # old endpoints. The offset/truncate dance lives in GroundAuditLog.audited_write, shared with
            # kg_ground; here the failure SIGNAL is info.rolled_back from write_nodes, not a caught exception.
            def _attempt():
                info = self.canon.write_nodes(touched, message=message, commit=False, merge=False)
                return (not info.rolled_back), info

            records = [(new_key, EpistemicState.UNVERIFIED.value, state, "agent")
                       for new_key, state in migrations]
            with self._critical_write():
                info = self._audit_log.audited_write(records, _attempt)
            if info.rolled_back:
                # the batch rolled back — do NOT unlink the old note, or the node would be lost entirely
                # (its migrating audit records were already truncated by GroundAuditLog.audited_write).
                return {"ok": False, "error": f"rename rolled back: {info.error}", "old": old, "new": new}
            try:
                self.canon.node_path(old).unlink(missing_ok=True)
            except OSError as e:  # the new note already landed; surface a structured error, not a raw raise
                return {"ok": False, "old": old, "new": new, "touched": [n.id for n in touched],
                        "error": self._scrub_error(f"rename wrote '{new}' but could not remove old '{old}': {e}")}
            from .canon import _git, _git_ok
            if _git_ok(self.canon.root):
                # stage only what this rename touched — the rewritten notes + the removed old note —
                # instead of `git add -A` re-scanning the whole working tree per rename (server-9).
                paths = [str(self.canon.node_path(n.id)) for n in touched]
                paths.append(str(self.canon.node_path(old)))
                _git(self.canon.root, "add", "--", *paths, check=False)
                # Scope the commit to THIS operation's paths (not a bare `git commit`, which would sweep
                # any externally-staged files in the user's project repo into an engine commit) — mirrors
                # the already-scoped `git add` (review: unscoped-commit-sweeps-staged-index).
                _git(self.canon.root, "commit", "-m", message, "--allow-empty", "--", *paths, check=False)
            return {"ok": True, "old": old, "new": new, "touched": [n.id for n in touched]}
        finally:
            self.canon._release_lock()

    @staticmethod
    def _merge_edge_pair(a: Edge, b: Edge) -> Edge:
        """Coalesce two edges that collide on ONE canonical id into a single edge — the dedup step of
        kg_merge — deterministically and WITHOUT forging, upgrading, or inventing a verdict/span.

        The merged epistemic_state is whichever of the two ranks higher (failed/rejected sticky as
        never-pruned negative information §1.7, else grounded > unverified), so the state is ALWAYS one
        a real edge already held. The verbatim span and verdict note are kept non-empty (never invented),
        and the verdict attribution (`verdict_by`/`verdict_at`) travels WITH the winning state so a
        grounded/failed edge is never left as a verdict floating over empty support (§1.8). The function
        is order-insensitive: swapping (a, b) yields the same merged edge."""
        ra, rb = _MERGE_STATE_RANK[a.epistemic_state], _MERGE_STATE_RANK[b.epistemic_state]
        if ra != rb:
            hi, lo = (a, b) if ra > rb else (b, a)
        else:
            # same state — keep the record carrying real evidence so its span + verdict stay paired.
            hi, lo = ((a, b) if _MERGE_PROV_RANK[a.provenance] >= _MERGE_PROV_RANK[b.provenance]
                      else (b, a))
        state = hi.epistemic_state
        # Keep a non-empty verbatim span, preferring the winning-state edge's (the span its verdict
        # cited). A surviving real span IS span-present; otherwise keep the stronger spanless provenance.
        span = hi.span or lo.span
        if span:
            provenance = Provenance.SPAN_PRESENT
        else:
            provenance = (a.provenance if _MERGE_PROV_RANK[a.provenance] >= _MERGE_PROV_RANK[b.provenance]
                          else b.provenance)
        verdict_by, verdict_at = ((hi.verdict_by, hi.verdict_at)
                                  if state in GROUNDABLE_STATES else (None, None))
        return Edge(source=hi.source, target=hi.target, relation=hi.relation,
                    provenance=provenance, authored_by=hi.authored_by, epistemic_state=state,
                    span=span, source_file=hi.source_file or lo.source_file,
                    confidence=hi.confidence, confidence_score=hi.confidence_score,
                    verdict_by=verdict_by, verdict_at=verdict_at, notes=(hi.notes or lo.notes))

    def _rewrite_dedup_edges(self, edges: "list[Edge]", frm: str, into: str, report: dict):
        """Rewrite every `frm`→`into` endpoint on a node's edge list, recompute each deterministic id,
        DROP self-loops (a rewrite that collapsed source==target), and DEDUP edges that now share one
        canonical id via `_merge_edge_pair`. Two edges can only collide on an id iff they share a source
        (edge_id is a function of source), and a node file holds only its own source's edges — so a
        collision is always within ONE file, which is why this dedup is per-node. Mutates `report`'s
        counters and returns (deduped_edges, changed)."""
        from .model import edge_id
        survivors: dict[str, Edge] = {}
        changed = False
        for e in edges:
            rewritten = (e.source == frm) or (e.target == frm)
            if e.source == frm:
                e.source = into
            if e.target == frm:
                e.target = into
            e.id = edge_id(e.source, e.relation, e.target)
            if rewritten:
                report["edges_rewritten"] += 1
                changed = True
            if e.source == e.target:  # the rewrite collapsed an endpoint pair into a self-loop
                # Negative information is NEVER pruned (§1.7): a failed/rejected edge lying directly
                # between the two merged nodes must survive the merge as a degenerate self-loop so its
                # verdict + span stay in falsification_counters. Only a positive/unverified self-loop is
                # discarded (review: merge-selfloop-drops-negative-info). A preserved negative self-loop
                # falls through to the survivors/dedup path below (its migrating audit record is emitted
                # by kg_merge's precisely-sized `migrations` set, since its id changed).
                if e.epistemic_state not in (EpistemicState.FAILED, EpistemicState.REJECTED):
                    report["self_loops_dropped"].append(e.id)
                    changed = True
                    continue
                changed = True
            prev = survivors.get(e.id)
            if prev is None:
                survivors[e.id] = e
            else:
                survivors[e.id] = self._merge_edge_pair(prev, e)
                report["edges_deduped"].append(
                    {"id": e.id, "state": survivors[e.id].epistemic_state.value})
                changed = True
        return list(survivors.values()), changed

    def kg_merge(self, from_id: str, into_id: str, *, message: str = "kg_merge") -> dict:
        """Merge node `from_id` INTO `into_id`: rewrite every edge endpoint referencing `from_id` to
        `into_id`, dedup edges that then collide on one canonical id (negative-info-sticky, never forging
        a verdict), drop the self-loops the rewrite creates, and RETIRE `from_id`. A DELIBERATE merge —
        deliberately a distinct verb from kg_rename, which stays strict (errors on a target collision) so
        a name clash can never silently fold two concepts together. Operates on the CANON only (never the
        projection seam); the reconciler re-attaches surviving verdicts to their new ids (§1.8)."""
        frm, into = slug(from_id), slug(into_id)
        if frm == into:
            return {"ok": False, "error": "cannot merge a node into itself", "from": frm, "into": into}
        # Acquire the single-writer lease FIRST, then read everything FRESH under the lease — same
        # ordering as kg_rename/kg_ground (F17/L5): reading before locking would let a concurrent
        # cross-process verdict on a sibling edge be clobbered by our verbatim (merge=False) write.
        # bounded-BLOCKING acquire (mirrors kg_write) so brief cross-process contention serializes rather
        # than failing this writer outright (review: writers-use-nonblocking-lock).
        try:
            self.canon._acquire_lock()
        except RuntimeError:
            return {"ok": False, "error": "canon vault is locked by another live session",
                    "from": frm, "into": into}
        try:
            if not self.canon.exists(frm):
                return {"ok": False, "error": "source node not found", "from": frm, "into": into}
            if not self.canon.exists(into):
                return {"ok": False, "error": "target node not found", "from": frm, "into": into}
            try:
                from_node = self.canon.read_node(frm)   # corrupt/invalid-UTF-8 note → structured error
                into_node = self.canon.read_node(into)
            except Exception as e:  # noqa: BLE001 — surface as a structured error, not an MCP exception
                return {"ok": False, "error": self._scrub_error(f"node unreadable: {e}"),
                        "from": frm, "into": into}
            # Typing safety: keep `into`'s node_type/label, but REFUSE a merge that would silently
            # overwrite one DECLARED type with a different one — a wrong merge must not corrupt typing.
            # An undeclared-type placeholder on either side is not a conflict (it carries no commitment).
            if (from_node.node_type != into_node.node_type
                    and from_node.node_type != UNDECLARED_TYPE
                    and into_node.node_type != UNDECLARED_TYPE):
                return {"ok": False, "error": "node_type conflict — refusing to merge",
                        "from_type": from_node.node_type, "into_type": into_node.node_type,
                        "from": frm, "into": into}

            others = [n for n in self.canon.all_nodes() if n.id not in (frm, into)]
            # Snapshot every (edge_id, state) present BEFORE the rewrite. We emit a migrating audit
            # record ONLY for a surviving policed edge whose (id, state) is NEW — i.e. its verdict now
            # sits where the audit history can't justify it (the rewrite changed its id, OR a dedup
            # lifted the state at an existing id). An edge whose (id, state) is unchanged already has its
            # baseline record, so we leave no spurious spendable record behind (mirrors kg_rename's
            # precisely-sized migration set; §1.8).
            pre_states = {(e.id, e.epistemic_state.value)
                          for n in [from_node, into_node, *others] for e in n.edges}

            report = {"edges_rewritten": 0, "edges_deduped": [], "self_loops_dropped": []}
            # `into` absorbs its own edges PLUS every edge sourced at `from` (all of which rewrite onto
            # `into`); rewrite + dedup the combined list as one file.
            into_node.edges, _ = self._rewrite_dedup_edges(
                list(into_node.edges) + list(from_node.edges), frm, into, report)
            touched = [into_node]
            for n in others:
                n.edges, changed = self._rewrite_dedup_edges(n.edges, frm, into, report)
                if changed:
                    touched.append(n)

            migrations = [(e.id, e.epistemic_state.value)
                          for e in (into_node.edges + [e for n in others for e in n.edges])
                          if e.epistemic_state in GROUNDABLE_STATES
                          and (e.id, e.epistemic_state.value) not in pre_states]
            records = [(eid, EpistemicState.UNVERIFIED.value, state, "agent")
                       for eid, state in migrations]

            def _attempt():
                # merge=False: every endpoint is already rewritten + deduped, so re-merging would
                # re-introduce the pre-rewrite edges (different id → not deduped) — same posture as
                # kg_rename. The failure SIGNAL is info.rolled_back, not a caught exception.
                info = self.canon.write_nodes(touched, message=message, commit=False, merge=False)
                return (not info.rolled_back), info

            with self._critical_write():
                info = self._audit_log.audited_write(records, _attempt)
            if info.rolled_back:
                # the batch rolled back — do NOT unlink `from` (its migrating records were already
                # truncated by audited_write); the graph is left exactly as it was.
                return {"ok": False, "error": f"merge rolled back: {info.error}",
                        "from": frm, "into": into}
            try:
                self.canon.node_path(frm).unlink(missing_ok=True)  # retire the now-empty source node
            except OSError as e:
                return {"ok": False, "from": frm, "into": into, "touched": [n.id for n in touched],
                        "error": self._scrub_error(f"merge wrote '{into}' but could not remove old '{frm}': {e}")}
            from .canon import _git, _git_ok
            if _git_ok(self.canon.root):
                # stage only the rewritten notes + the removed source note (not a whole-tree `git add -A`).
                paths = [str(self.canon.node_path(n.id)) for n in touched]
                paths.append(str(self.canon.node_path(frm)))
                _git(self.canon.root, "add", "--", *paths, check=False)
                # Scope the commit to THIS operation's paths (not a bare `git commit`, which would sweep
                # any externally-staged files in the user's project repo into an engine commit) — mirrors
                # the already-scoped `git add` (review: unscoped-commit-sweeps-staged-index).
                _git(self.canon.root, "commit", "-m", message, "--allow-empty", "--", *paths, check=False)
            return {"ok": True, "from": frm, "into": into, "touched": [n.id for n in touched],
                    "edges_rewritten": report["edges_rewritten"],
                    "edges_deduped": report["edges_deduped"],
                    "self_loops_dropped": report["self_loops_dropped"],
                    "nodes": len(others) + 1,
                    "edges": sum(len(n.edges) for n in others) + len(into_node.edges)}
        finally:
            self.canon._release_lock()

    def kg_metrics(self) -> dict:
        # When the derived index is already fresh, serve counts from it with O(1) SQL instead of
        # re-parsing the whole canon (server-3). kg_metrics is not itself a projection trigger, so when
        # the index is stale we fall back to the authoritative canon parse rather than forcing a project.
        try:
            if self.projector.db_path.exists() and not self.projector.is_stale():
                con = self.projector._ro()
                try:
                    n = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                    e = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                    by_state = dict(con.execute(
                        "SELECT epistemic_state, COUNT(*) FROM edges GROUP BY epistemic_state"))
                finally:
                    con.close()
                return {"nodes": n, "edges": e, "edges_by_epistemic_state": by_state}
        except Exception as e:  # noqa: BLE001 — any index hiccup falls back to the canon parse below
            logger.debug("metrics index read failed (%s); falling back to canon parse", e)
        nodes = self.canon.all_nodes()
        edges = [e for n in nodes for e in n.edges]
        by_state: dict = {}
        for e in edges:
            by_state[e.epistemic_state.value] = by_state.get(e.epistemic_state.value, 0) + 1
        return {"nodes": len(nodes), "edges": len(edges), "edges_by_epistemic_state": by_state}

    @staticmethod
    def _split_sections(text: str) -> "list[tuple[str, str]]":
        """Split a source document into (heading, body) by level-2 `## ` headings — the SAME unit
        `/kg-build` extracts per subagent. Text before the first `##` is the `(preamble)`. `### ` and
        deeper stay inside their `##` section's body."""
        sections: list[tuple[str, str]] = []
        title, buf = None, []
        for line in text.splitlines():
            # `### `/deeper never satisfy startswith("## ") (the 3rd char is '#', not ' '), so they fall to
            # the else and stay in the section body — no extra guard needed.
            if line.startswith("## "):
                if title is not None or buf:
                    sections.append((title or "(preamble)", "\n".join(buf)))
                title, buf = line[3:].strip(), []
            else:
                buf.append(line)
        if title is not None or buf:
            sections.append((title or "(preamble)", "\n".join(buf)))
        return sections

    def _coverage(self, edges) -> dict:
        """Which configured source files / `##` sections already have at least one ANCHORED (span-present)
        edge — the resume signal: a section with no covered span hasn't been extracted yet. Reads the
        source text (cheap, memoized) + the canon spans only; never the derived layer."""
        try:
            texts = self.source_set().texts  # {basename → raw_text} (a property)
        except Exception as e:  # noqa: BLE001 — a source-read hiccup degrades coverage, never crashes kg_status
            return {"files": [], "sections": [], "note": f"source unavailable ({type(e).__name__})"}
        # DEDUP the normalized spans (many edges re-cite the same span) so the scan below is bounded by
        # DISTINCT spans, then INVERT the matching: walk each span once, marking the file + every section
        # it anchors and SKIPPING anything already covered, so a covered section/file is never re-scanned
        # and the loop short-circuits once a file is fully covered. Identical result to the old per-section
        # `any(sp in body for sp in spans)` (covered iff some span is a substring), without the
        # files×sections×spans worst case.
        spans = {s for s in (normalize_text(e.span) for e in edges if e.span and e.span.strip()) if s}
        files, sections = [], []
        for fname, raw in texts.items():
            secs = self._split_sections(raw)
            norm_bodies = [normalize_text(body) for _title, body in secs]
            covered_flags = [False] * len(secs)
            norm_file = normalize_text(raw)
            file_covered = False
            remaining = len(secs)  # sections still uncovered
            for sp in spans:
                if file_covered and remaining == 0:
                    break  # nothing left to discover in this file
                if not file_covered and sp in norm_file:
                    file_covered = True
                if remaining:
                    for i, nb in enumerate(norm_bodies):
                        if not covered_flags[i] and sp in nb:
                            covered_flags[i] = True
                            remaining -= 1
            for (title, _body), covered in zip(secs, covered_flags):
                sections.append({"file": fname, "title": title, "covered": covered})
            files.append({"file": fname, "covered": file_covered,
                          "sections": len(secs), "covered_sections": sum(covered_flags)})
        return {"files": files, "sections": sections}

    def kg_status(self) -> dict:
        """A cheap, projection-FREE status + coverage probe (resume a partial build after any transport
        hiccup without grepping the filesystem). Reads ONLY the canon (and the source text for coverage) —
        it never triggers or refreshes the derived layer, so it is safe and instant even mid-build while a
        projection would be expensive. Reports node/edge counts, edges by epistemic state, the
        still-`unverified` grounding-queue size, and which source files/`##` sections already have an
        anchored edge. `derived_present` is a path-existence check only (no db open); `projection_degraded`
        echoes any last reprojection failure (a read, not this probe, sets it)."""
        nodes = self.canon.all_nodes()
        edges = [e for n in nodes for e in n.edges]
        by_state = dict(Counter(e.epistemic_state.value for e in edges))
        nodes_by_state = dict(Counter(n.epistemic_state.value for n in nodes))
        return {
            "ok": True,
            "version": __version__,
            "nodes": len(nodes),
            "edges": len(edges),
            "edges_by_epistemic_state": by_state,
            "nodes_by_epistemic_state": nodes_by_state,
            "unverified_edges": by_state.get(EpistemicState.UNVERIFIED.value, 0),
            "coverage": self._coverage(edges),
            "derived_present": self.projector.db_path.exists(),
            "projection_degraded": self._projection_degraded,
        }

    def _failure_ids(self, G=None) -> set:
        """Forward edge ids in failure memory (rejected/failed). The generators also check the reverse,
        so forward ids suffice for invariant 5 (PLAN §13: failure memory binds generation).

        When the caller already loaded the derived graph (kg_generate/kg_operate both do, right before
        calling this), pass it in to derive the ids from the in-memory edges instead of re-parsing the
        whole canon (server-6). The index keeps failure memory (§1.7 never prunes it), so the set is
        identical; EpistemicState subclasses str, so the string compare matches."""
        from .model import FAILURE_STATES
        if G is not None:
            fail = {s.value for s in FAILURE_STATES}
            return {d.get("id") for _, _, d in G.edges(data=True) if d.get("epistemic_state") in fail}
        return {e.id for e in self.canon.all_edges() if e.epistemic_state in FAILURE_STATES}

    def kg_generate(self, mechanism: str = "bridge", k: int = 10, second_graph: str | None = None) -> dict:
        """Generate hypothesized candidates from the derived graph (PLAN Stage 3 — the generative
        engine). Projects if stale, reads precomputed ranks O(1), dispatches to the chosen mechanism(s)
        (`bridge|seed|compression|regroup|transplant|ensemble`, or `all`/`default`), and returns ranked
        candidates. **READ-ONLY** — it never writes the canon; `/kg-generate` routes the candidates
        through the propose lane (`kg_propose`). Generate offensively; grounding judges later."""
        from .generate import run_generators
        self._ensure_projected()
        G = self.projector.load_graph()
        corpus = self.projector._corpus()
        failures = self._failure_ids(G)
        gate_on = int(next((G.nodes[n].get("gate_on", 0) for n in G.nodes()), 0) or 0)  # `or 0`: tolerate gate_on=None
        G2, note = None, ""
        if second_graph:
            try:
                G2 = self._second_graph(second_graph)
            except Exception as e:  # noqa: BLE001 — a bad second graph degrades, never crashes
                note = f"second_graph could not be loaded ({e}); ensemble degraded to regroup"
        if not note and G2 is None and mechanism in ("ensemble", "all"):
            note = "no second construction supplied; ensemble degraded to regroup (run /kg-perturb to supply one)"
        cands = run_generators(G, mechanism, pack=self.pack, corpus=corpus, failures=failures,
                               k=k, second_graph=G2)
        # Echo projection_degraded like the sibling reads so a caller can tell "no candidates because the
        # graph is genuinely empty" from "no candidates because projection failed/was contended"
        # (review: generative-reads-omit-degraded-flag).
        return self._with_degraded({"mechanism": mechanism, "k": int(k), "gate_on": gate_on,
                                    "count": len(cands), "candidates": [c.to_dict() for c in cands],
                                    "note": note})

    def _second_graph(self, path: str):
        """Load a SECOND construction's graph.json into a NetworkX graph (raises on failure)."""
        from .generate import load_second_graph
        return load_second_graph(path)

    def kg_ensemble_graph(self, path: str) -> dict:
        """Load and summarise a SECOND construction's graph.json (PLAN Stage 7 — the §9/§15 ensemble /
        perturb path). Confirms a second construction projected before cross-generating against it via
        kg_generate(mechanism="ensemble", second_graph=<path>). Returns {ok, nodes, edges, path} or
        {ok: False, error}."""
        try:
            G2 = self._second_graph(path)
        except Exception as e:  # noqa: BLE001 — a missing/bad second graph is a structured error
            return {"ok": False, "error": str(e), "path": path}
        return {"ok": True, "path": path, "nodes": G2.number_of_nodes(), "edges": G2.number_of_edges()}

    def kg_absorption(self) -> dict:
        """Score the absorption window of grounded-from-hypothesized nodes (§14, PLAN Stage 5): how long
        each stayed perturbing before the graph renormalised. Reads the current derived graph plus the
        generation timeline at `derived/generations.json` — a `{generation: int, tracked: {id:
        {introduced_at, introduced_degree, mechanism}}}` ledger the /kg-generate command appends to.
        Returns per-node {half_life, status ∈ fertile|absorbed|isolated} so the slate can prefer the
        fertile middle. With no ledger yet, returns an empty result with a note (never an error)."""
        from .harness import absorption
        self._ensure_projected()
        try:
            data = json.loads(self.projector.graph_path.read_text(encoding="utf-8")) \
                if self.projector.graph_path.exists() else {"nodes": [], "links": []}
        except (ValueError, OSError):
            data = {"nodes": [], "links": []}
        hist_path = self.projector.derived / "generations.json"
        history, now = {}, None
        if hist_path.exists():
            try:
                blob = json.loads(hist_path.read_text(encoding="utf-8"))
                if isinstance(blob, dict):
                    history = blob.get("tracked", {}) if "tracked" in blob else blob
                    now = blob.get("generation")
            except (ValueError, OSError):
                history = {}
        result = absorption(data, history, now=now)
        summary = {s: sum(1 for v in result.values() if v["status"] == s)
                   for s in ("fertile", "absorbed", "isolated")}
        return self._with_degraded({"tracked": len(result), "summary": summary, "nodes": result,
                "note": ("" if history else
                         "no generations.json yet — run /kg-generate to start tracking the absorption window")})

    def kg_operate(self, op: str, *, target: str | None = None, label: str = "", body: str = "",
                   members=None, k: int | None = None) -> dict:
        """Run one of the four endo operations (§8, PLAN Stage 4), persisting the result through the
        propose lane. collapse → compression node + collapses_into edges; explode → latent facet
        children; regroup → §8 re-partition bridges; open → a new primitive + attachment points. The
        write goes through kg_propose, so it lands hypothesized/unverified with no span — never a
        verdict, never a forged text anchor."""
        from . import operations as ops
        op = (op or "").lower()
        fn = ops.DISPATCH.get(op)
        if fn is None:
            return {"ok": False, "error": f"unknown op {op!r}; expected collapse|explode|regroup|open"}
        self._ensure_projected()
        G = self.projector.load_graph()
        if op == "collapse":
            payload, info = fn(G, target=target, members=members, label=label, body=body)
        elif op == "explode":
            payload, info = fn(G, target=target, k=k, label=label, body=body)
        elif op == "regroup":
            payload, info = fn(G, failures=self._failure_ids(G), k=k or ops.DEFAULT_REGROUP_K)
        else:  # open
            payload, info = fn(G, label=label, body=body, k=k or ops.DEFAULT_OPEN_POINTS)
        if not payload or not (payload.get("nodes") or payload.get("edges")):
            return {"ok": False, "op": op, "error": "no structure to operate on", "info": info}
        result = self.kg_propose(payload, message=f"kg_operate:{op}")
        result.update({"ok": True, "op": op, "info": info})
        return result

    # ---- read surface (projects if stale, then reads precomputed ranks)
    def _ensure_projected(self) -> None:
        """Project-if-stale on the read path — and NEVER raise. A projection failure (a sqlite hiccup, a
        native-dep blowup in community detection, a corrupt derived db) must DEGRADE a read, not crash the
        tool: log it, remember the reason in `_projection_degraded`, and make sure an empty-schema'd
        derived layer exists so the read tools return canon-empty data with the degraded flag instead of
        blowing up on a missing table. Writes never come through here — kg_write/kg_propose/kg_ground/
        kg_rename touch only the canon — so projection can never block or fail a write (defense #6)."""
        try:
            report = None
            if not self.projector.db_path.exists() or self.projector.is_stale():
                report = self.projector.project()
            # A cold-start reproject that could NOT take the canon lease (another process holds it) bails
            # out having synthesized an EMPTY/stale derived layer and returns contended=True. Surface that
            # as a degraded read so an empty result is not mistaken for a genuinely empty graph — it
            # self-heals on the next uncontended read (review: contended-projection-looks-empty).
            if report is not None and getattr(report, "contended", False):
                self._projection_degraded = ("projection contended: another process holds the canon lease; "
                                             "serving an empty/stale derived layer (refreshes on next read)")
            else:
                self._projection_degraded = None
        except Exception as e:  # noqa: BLE001 — a projection failure degrades the read, never crashes it
            self._projection_degraded = f"{type(e).__name__}: {e}"
            logger.warning("projection failed (%s); serving degraded derived layer", e, exc_info=True)
            self._ensure_degraded_db()

    def _ensure_degraded_db(self) -> None:
        """Best-effort: guarantee a schema'd (possibly empty/stale) derived layer exists so a read after a
        projection failure returns data instead of crashing on a missing table. Mirrors the projector's
        cold-contended path (an idempotent CREATE TABLE IF NOT EXISTS + an empty graph.json)."""
        try:
            import networkx as nx
            from .projector import _atomic_write, _node_link_data
            if not self.projector.db_path.exists() or self.projector._schema_outdated():
                self.projector._connect().close()
            if not self.projector.graph_path.exists():
                _atomic_write(self.projector.graph_path, json.dumps(_node_link_data(nx.MultiDiGraph())))
        except Exception as e:  # noqa: BLE001 — purely defensive; a read that still can't project errors via _tool_result
            logger.debug("could not materialise a degraded derived layer (%s)", e)

    def _with_degraded(self, result):
        """Annotate a dict read result with the projection-degraded reason, when set, so a caller can tell
        "the derived layer is stale/unavailable" apart from a genuinely empty graph. Non-dict results
        (lists, None) pass through untouched."""
        if isinstance(result, dict) and self._projection_degraded:
            result = {**result, "projection_degraded": self._projection_degraded}
        return result

    # Free-text fields of a read result that may quote canon/source content (a verbatim edge span, a
    # verdict note, a node body). kg_write deliberately stores the ORIGINAL (un-scrubbed) span in the
    # canon (§1.9 restore protects the egress, not the local canon), so a secret that was scrubbed BEFORE
    # extraction lives in the canon span — and must be re-scrubbed on the READ path before it crosses back
    # to the model. Structural fields (ids/relation/type/axes) are deliberately excluded so referential
    # integrity is preserved (review: reads-return-canon-spans-unscrubbed).
    _EGRESS_TEXT_KEYS = frozenset({"span", "notes", "note", "body", "support_note"})

    def _scrub_egress(self, obj):
        """Re-run the §1.9 egress scrub over the free-text fields of a read result before it returns to
        the session, so a secret restored into a canon span can't round-trip to the model on a read. Uses
        a THROWAWAY Scrubber at the engine's configured sensitivity (never pollutes the session's
        write-restore map `_scrub_map`); a no-op on ordinary conceptual text (nothing matches). Recurses
        through nested dicts/lists (items[]/hypotheses[]/edges[]/…)."""
        scrubber = Scrubber(self.sensitivity)

        def _walk(x):
            if isinstance(x, dict):
                return {k: (scrubber.scrub(v)[0]
                            if (k in self._EGRESS_TEXT_KEYS and isinstance(v, str) and v)
                            else _walk(v))
                        for k, v in x.items()}
            if isinstance(x, list):
                return [_walk(v) for v in x]
            return x

        return _walk(obj)

    @property
    def _proj(self) -> Projector:
        """The lazy-projection read seam: ensure the derived layer is fresh, then return the projector.
        The single edit point for the projection trigger every pure read delegate goes through."""
        self._ensure_projected()
        return self.projector

    def get_node(self, node_id: str) -> dict | None:
        res = self._proj.get_node(node_id)
        # On a degraded (empty/stale) derived layer a real canon node looks like a genuine miss; surface
        # the flag on the miss too so a caller can tell "not found" from "couldn't project" (review-M2).
        if res is None and self._projection_degraded:
            return {"error": "not found", "projection_degraded": self._projection_degraded}
        return self._with_degraded(self._scrub_egress(res))

    def get_neighbors(self, node_id: str, relation: str | None = None) -> list:
        # Returns a LIST, which can't carry the projection_degraded flag without changing the tool's
        # shape; the degraded state is observable via the sibling reads (get_node/kg_context/query_graph/
        # kg_status) that DO carry it. _ensure_projected (via _proj) still degrades-not-raises here.
        return self._scrub_egress(self._proj.get_neighbors(node_id, relation=relation))

    def shortest_path(self, source: str, target: str):
        return self._scrub_egress(self._proj.shortest_path(source, target))

    def explain_path(self, nodes: list[str]) -> dict:
        """Trace the associative chain connecting `nodes` over GROUNDED edges only (read-only egress, §2).
        Returns the ordered node `path`, the grounded `edges` used (relation + span, for audit), and an
        ADVISORY `leap` = the path edge-count — never a verdict, never written, never folded into a score
        (G1/G4). For >2 nodes the visiting order comes from a deterministic nearest-neighbour walk (a TSP
        approximation) over the grounded shortest-path closure — byte-stable across processes by a
        (distance, id) tie-break, no external solver, no new dependency. The result is EMPTY (path=[],
        leap=null) with a `reason` when no fully-grounded path exists — itself informative: the concepts
        are joined only through unverified/hypothesized/refuted links, or not at all. Mirrors
        shortest_path's `projection_degraded` surfacing so an empty result is never confused with a
        degraded projection."""
        import itertools
        from collections import deque

        import networkx as nx

        G = self._proj.load_graph()
        # the grounded-ONLY undirected graph: an undirected pair {u,v} exists iff at least one parallel
        # edge between them (either direction) is epistemic_state == grounded. Carry ONE representative
        # grounded edge's relation+span — deterministically the smallest edge id — for the audit trail.
        # unverified / hypothesized / failed / rejected edges are excluded ENTIRELY: routing a chain
        # through them would manufacture a false "explanation", defeating the auditability purpose (§2).
        Gg = nx.Graph()
        Gg.add_nodes_from(G.nodes())
        reps: dict = {}  # frozenset({u,v}) -> (edge_id, relation, span) representative grounded edge
        for u, v, d in G.edges(data=True):
            if u == v or d.get("epistemic_state") != EpistemicState.GROUNDED.value:
                continue
            key = frozenset((u, v))
            eid = d.get("id", "") or ""
            prior = reps.get(key)
            if prior is None or eid < prior[0]:
                reps[key] = (eid, d.get("relation", "") or "", d.get("span", "") or "")
        for key, (_eid, rel, span) in reps.items():
            a, b = sorted(key)
            Gg.add_edge(a, b, relation=rel, span=span)

        def _grounded_path(s, t):
            """Deterministic shortest path s->t over Gg (sorted-neighbour predecessor BFS); None when no
            fully-grounded path exists."""
            if s == t:
                return [s]
            if s not in Gg or t not in Gg:
                return None
            pred = {s: s}
            q = deque([s])
            while q:
                cur = q.popleft()
                for nb in sorted(Gg.neighbors(cur)):  # sorted -> byte-stable path among equal-length ties
                    if nb in pred:
                        continue
                    pred[nb] = cur
                    if nb == t:
                        path = [t]
                        while path[-1] != s:
                            path.append(pred[path[-1]])
                        path.reverse()
                        return path
                    q.append(nb)
            return None

        def _unreachable(reason):
            return self._with_degraded({"path": [], "edges": [], "leap": None,
                                        "grounded_only": True, "reason": reason})

        uniq = sorted(set(str(n) for n in (nodes or [])))
        if not uniq:
            return _unreachable("no nodes requested")
        missing = [n for n in uniq if n not in Gg]
        if missing:
            return _unreachable(f"node not in graph: {missing[0]}")
        if len(uniq) == 1:
            return self._with_degraded({"path": [uniq[0]], "edges": [], "leap": 0, "grounded_only": True})

        # cache oriented grounded shortest paths between requested concepts (the BFS is deterministic).
        seg_cache: dict = {}

        def _seg(a, b):
            if (a, b) not in seg_cache:
                p = _grounded_path(a, b)
                seg_cache[(a, b)] = p
                if p is not None:
                    seg_cache[(b, a)] = list(reversed(p))
            return seg_cache[(a, b)]

        if len(uniq) == 2:
            full = _seg(uniq[0], uniq[1])
            if full is None:
                return _unreachable(f"no fully-grounded path between {uniq[0]} and {uniq[1]}")
        else:
            # every requested concept must be mutually reachable over grounded edges; report the FIRST
            # offending pair (combinations of the sorted set -> deterministic). Build the metric closure.
            dist: dict = {}
            for a, b in itertools.combinations(uniq, 2):
                seg = _seg(a, b)
                if seg is None:
                    return _unreachable(f"no fully-grounded path between {a} and {b}")
                dist[(a, b)] = dist[(b, a)] = len(seg) - 1
            # order the concepts by a DETERMINISTIC nearest-neighbour walk over the grounded shortest-path
            # closure (a TSP-path approximation): start at the smallest id, then repeatedly take the
            # closest unvisited concept, tie-broken by id. The (distance, id) key is a TOTAL order, so
            # `min` is byte-stable ACROSS PROCESSES — unlike networkx greedy_tsp, whose internal
            # `min(set(...))` tie-breaks on hash-randomized set iteration order (a real G6 hazard here,
            # since the grounded closure's unit-weight distances tie pervasively and server restarts are
            # routine). Then expand each consecutive pair into its grounded shortest path.
            order = [uniq[0]]
            remaining = set(uniq[1:])
            while remaining:
                cur = order[-1]
                nxt = min(remaining, key=lambda n: (dist[(cur, n)], n))
                order.append(nxt)
                remaining.discard(nxt)
            full = []
            for a, b in zip(order, order[1:]):
                seg = _seg(a, b)
                full += seg if not full else seg[1:]

        # collect the grounded edges along the full chain (relation+span per hop, for audit). Every
        # consecutive pair is a Gg edge by construction; guard defensively all the same.
        edges_used = []
        for a, b in zip(full, full[1:]):
            d = Gg.get_edge_data(a, b)
            if d is None:
                return _unreachable(f"no fully-grounded path between {a} and {b}")
            edges_used.append({"source": a, "target": b,
                               "relation": d.get("relation", ""), "span": d.get("span", "")})
        # re-scrub the grounded spans before egress (a secret restored into a canon span must not
        # round-trip to the model — review: reads-return-canon-spans-unscrubbed).
        return self._with_degraded(self._scrub_egress(
            {"path": full, "edges": edges_used, "leap": len(edges_used), "grounded_only": True}))

    def query_graph(self, **kw) -> dict:
        return self._with_degraded(self._scrub_egress(self._proj.query_graph(**kw)))

    def kg_context(self, query: str | None = None, budget: int = 2000) -> dict:
        return self._with_degraded(self._scrub_egress(self._proj.kg_context(query, budget=budget)))

    def kg_agenda(self, *, limit: int = 5) -> dict:
        return self._with_degraded(self._proj.kg_agenda(limit=limit))

    def kg_export(self, kind: str = "all") -> dict:
        """Render the human-facing artifacts (R1): a self-contained `graph.html` + `GRAPH_REPORT.md` under
        the derived dir. Read-only — projects-if-stale, then consumes only the derived layer; never writes
        the canon and never `_atomic_write`s graph.json/index.sqlite."""
        self._ensure_projected()
        from . import export as _export
        return self._with_degraded(_export.export(self, kind=kind))


# --------------------------------------------------------------------------- MCP wiring


def build_engine_from_env(*, project=None, data=None, source=None, pack=None) -> KGEngine:
    """Construct a KGEngine from environment config, with optional explicit overrides (CLI flags win
    over env). All resolution — project dir, source, pack auto-discovery, and the flood rate limit —
    lives here so every caller (MCP server, headless backend) gets identical behavior."""
    # Treat an empty OR unsubstituted `${...}` env value the same as unset. When a `${user_config.*}`
    # placeholder is never substituted — e.g. `source_path`, which has no default in plugin.json — Claude
    # Code passes the literal `${user_config.source_path}` through `.mcp.json`. Taking that as a real path
    # silently breaks the engine: `source_text()` reads a non-existent file and returns "", so every agent
    # edge fails span verification (`span-not-in-source`). Mirrors `bootstrap._clean` / `launch_server.clean`,
    # which strip the same values; without it the documented `examples/source.md` fallback never fires.
    # `_clean_env` is the module-level form of the old local `_env`, shared with the logging path
    # (resolve_data_dir) so the server log lands in the SAME dir the engine resolves for the derived layer.
    _env = _clean_env
    project = project or _env("KG_PROJECT_DIR") or _env("CLAUDE_PROJECT_DIR") or os.getcwd()
    data = data or _env("KG_DATA")
    opt = lambda k, d=None: (os.environ.get(f"CLAUDE_PLUGIN_OPTION_{k}") or "").strip() or d  # noqa: E731
    src = source or opt("SOURCE_PATH") or _env("KG_SOURCE_PATH")
    if not src:
        # documented default: build/ground against the bundled example when nothing is configured
        guess = Path(project) / "examples" / "source.md"
        src = str(guess) if guess.exists() else None
    pack_path = pack or _env("KG_PACK_PATH")
    if not pack_path:
        guess = Path(project) / "pack" / "pack.yaml"
        pack_path = str(guess) if guess.exists() else None
    try:
        rate = float(os.environ["KG_MAX_EDGES_PER_KB"])
        if not math.isfinite(rate) or rate < 0:  # 'nan'/'inf'/negative would crash or disable the limiter
            rate = DEFAULT_MAX_EDGES_PER_KB
    except (KeyError, ValueError):
        rate = DEFAULT_MAX_EDGES_PER_KB
    return KGEngine(project, data, source_path=src, pack_path=pack_path,
                    sensitivity=opt("SENSITIVITY", "medium"), metrics_mode=opt("METRICS_MODE", "structure_only"),
                    max_edges_per_kb=rate)


def _tool_result(fn):
    """Uniform transport-error envelope for every MCP tool (finding: mixed-error-architecture). A RAISED
    exception (e.g. a mid-read sqlite/networkx error escaping a pure-read tool, or a BrokenPipeError /
    EOFError / ConnectionResetError on the stdio transport — all `Exception` subclasses) becomes a
    structured {ok:False, error, error_kind} result + a logged traceback, instead of bubbling into the
    transport serve loop and killing the process. The next request is served normally. SUCCESS returns
    pass through UNCHANGED — including the deliberate {ok:False} DOMAIN dispositions (a locked vault, a
    refused verdict) and the reads' own shapes ({path:...}, {error:"not found"}, lists, None): transport
    ok/error and domain disposition are two ORTHOGONAL axes, so the envelope never collapses a domain
    result into a transport error (the never-stall contract, §2.4/§4).

    It deliberately catches `Exception`, NOT `BaseException`: an `asyncio.CancelledError`,
    `KeyboardInterrupt`, or `SystemExit` MUST propagate so cooperative cancellation / shutdown still
    works (swallowing a CancelledError would hang the framework's cancel of that one request). Per-request
    cancellation already aborts only that request — the mcp serve loop isolates each handler in its own
    task and returns tool errors as messages (raise_exceptions=False) — so a cancelled call never takes
    the loop down. functools.wraps keeps the wrapped signature so FastMCP still builds the right tool
    schema, and the manifest scrape still recognises the `def`."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        wd = _WATCHDOG
        if wd is not None:
            wd.enter(fn.__name__)
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — uniform transport envelope; a tool must never crash the call
            # Log the RAW exception locally (server.log is a local diagnostic, not egress), but SCRUB the
            # returned `error` so a raised exception can't leak an absolute path or un-scrubbed canon
            # content back across the §1.9 egress to the session. `error_kind` (the type name) is verbatim.
            logger.warning("MCP tool %s raised %s: %s", fn.__name__, type(e).__name__, e, exc_info=True)
            return {"ok": False, "error": _scrub_error_text(str(e), sensitivity=_active_sensitivity()),
                    "error_kind": type(e).__name__}
        finally:
            if wd is not None:
                wd.exit()
    return wrapper


def _register(mcp, engine: KGEngine) -> None:
    # Expose the engine to the module-scope tool envelope so it scrubs error egress at the CONFIGURED
    # sensitivity (review: error-envelope-ignores-configured-sensitivity).
    global _ACTIVE_ENGINE
    _ACTIVE_ENGINE = engine

    @mcp.tool()
    @_tool_result
    def kg_ping() -> dict:
        """Health check: returns the engine version and configuration."""
        return engine.kg_ping()

    @mcp.tool()
    @_tool_result
    def kg_scrub(text: str | None = None) -> dict:
        """Egress PII/secret scrub (§1.9): redact a snippet (or the source) with consistent placeholders
        before handing text to a subagent; the canon later restores spans to the original."""
        return engine.kg_scrub(text)

    @mcp.tool()
    @_tool_result
    def kg_write(payload: dict, idempotency_key: str | None = None) -> dict:
        """Validate an extraction payload at the boundary and write accepted/demoted nodes & edges. The
        response carries a deterministic `receipt` (a hash of the payload's target ids); pass an
        `idempotency_key` to make a retry of a write whose transport response was lost a TRUE no-op that
        replays the identical receipt + dispositions (`idempotent_replay: True`) instead of a second pass.
        Without a key the write is still idempotent by canonical id — a re-send creates no duplicates."""
        return engine.kg_write(payload, idempotency_key=idempotency_key)

    @mcp.tool()
    @_tool_result
    def kg_propose(payload: dict) -> dict:
        """Propose hypothesized candidates (PLAN Stage 1: the propose lane). Forces every item to
        provenance=hypothesized (a discovery-mechanism proposal, no span needed) and REFUSES any
        span-present/inferred text claim with reason `propose-lane-text-claim` — text claims belong on
        kg_write. Candidates land `unverified`; only kg_ground (with support) can ever promote them."""
        return engine.kg_propose(payload)

    @mcp.tool()
    @_tool_result
    def kg_ground(target_id: str, verdict: str, kind: str = "edge", note: str = "",
                  support_span: str = "", support_note: str = "") -> dict:
        """Apply a grounding verdict (grounded|rejected|failed|obsolete) to an edge or node. Verdicts
        applied via this tool are always attributed to the agent — a human verdict cannot be forged
        through the tool surface (§1.4). To PROMOTE a hypothesized edge OR node to grounded you MUST supply
        support, which upgrades its provenance: `support_span` (a verbatim source substring → span-present)
        or `support_note` (an external citation → inferred); without either, the promotion is refused with
        `hypothesis-needs-support`. `note` is appended to the edge's `notes` and is **edge-only** — it is
        ignored for kind='node' (a Node has no notes field)."""
        return engine.kg_ground(target_id, verdict, by="agent", kind=kind, note=note,
                                support_span=support_span, support_note=support_note)

    @mcp.tool()
    @_tool_result
    def kg_rename(old_id: str, new_id: str) -> dict:
        """Rename a node and rewrite every edge endpoint referencing it. STRICT: refuses when `new_id`
        already exists (a name collision is never silently a merge — use kg_merge for that)."""
        return engine.kg_rename(old_id, new_id)

    @mcp.tool()
    @_tool_result
    def kg_merge(from_id: str, into_id: str) -> dict:
        """Deliberately MERGE node `from_id` into the existing node `into_id` (both must exist), then
        retire `from_id`. Rewrites every edge endpoint `from_id`→`into_id`; where that collides two edges
        on one canonical id they are DEDUPED — failed/rejected negative information is sticky and never
        pruned (§1.7), else grounded beats unverified, and the verbatim span + verdict note are kept;
        no verdict/span is ever forged, upgraded, or invented. Self-loops the rewrite creates are dropped.
        Keeps `into_id`'s node_type/label and REFUSES a merge across two different declared node_types.
        Returns the edges rewritten/deduped/dropped and the final counts."""
        return engine.kg_merge(from_id, into_id)

    @mcp.tool()
    @_tool_result
    def kg_metrics() -> dict:
        """Summary counts: nodes, edges, edges by epistemic state."""
        return engine.kg_metrics()

    @mcp.tool()
    @_tool_result
    def kg_status() -> dict:
        """Cheap, projection-FREE status + coverage probe — confirm build progress and RESUME a partial
        build after any transport hiccup without grepping the filesystem. Reads ONLY the canon (+ source
        text for coverage); never triggers/refreshes the derived layer. Returns node/edge counts, edges
        by epistemic state, the still-`unverified` grounding-queue size, and which source files/`##`
        sections already have an anchored edge. Unlike kg_metrics it never opens the derived db."""
        return engine.kg_status()

    @mcp.tool()
    @_tool_result
    def kg_generate(mechanism: str = "bridge", k: int = 10, second_graph: str | None = None) -> dict:
        """Generate hypothesized idea candidates from the graph's structure (PLAN Stage 3). Mechanisms:
        bridge (§2/§4), seed (§3 residual), compression (§7 new nodes), regroup (§8), transplant (§5),
        ensemble (§9) — or "all"/"default". READ-ONLY: candidates are proposals (provenance=hypothesized,
        no span); route them through kg_propose. Generate offensively; kg_ground judges later."""
        return engine.kg_generate(mechanism=mechanism, k=k, second_graph=second_graph)

    @mcp.tool()
    @_tool_result
    def kg_absorption() -> dict:
        """Absorption window (§14): for each grounded-from-hypothesized node, how long it stayed
        perturbing before the graph renormalised — {half_life, status ∈ fertile|absorbed|isolated}.
        Reads derived/generations.json (written by /kg-generate). Prefer the fertile middle."""
        return engine.kg_absorption()

    @mcp.tool()
    @_tool_result
    def kg_operate(op: str, target: str | None = None, label: str = "", body: str = "",
                   members: list[str] | None = None, k: int | None = None) -> dict:
        """The four endo operations (§8) that WRITE hypothesized structure via the propose lane:
        collapse (cluster→compression node + collapses_into), explode (node→latent facet children),
        regroup (persist §8 re-partition bridges), open (new primitive + attachment points). Everything
        lands hypothesized/unverified with no span — never a verdict, never a forged anchor.
        `members` names an explicit member set for collapse (else the cluster is inferred from target)."""
        return engine.kg_operate(op, target=target, label=label, body=body, members=members, k=k)

    @mcp.tool()
    @_tool_result
    def query_graph(node_type: str | None = None, relation: str | None = None,
                    epistemic_state: str | None = None, limit: int = 50) -> dict:
        """Query nodes/edges by type, relation, or epistemic state (ranked by precomputed degree)."""
        return engine.query_graph(node_type=node_type, relation=relation,
                                  epistemic_state=epistemic_state, limit=limit)

    @mcp.tool()
    @_tool_result
    def get_node(node_id: str) -> dict:
        """Fetch a node with its incident edges."""
        return engine.get_node(node_id) or {"error": "not found"}

    @mcp.tool()
    @_tool_result
    def get_neighbors(node_id: str, relation: str | None = None) -> list:
        """Edges incident to a node, optionally filtered by relation."""
        return engine.get_neighbors(node_id, relation)

    @mcp.tool()
    @_tool_result
    def shortest_path(source: str, target: str) -> dict:
        """Shortest path between two nodes over the derived graph."""
        out = {"path": engine.shortest_path(source, target)}
        # surface the degraded signal so an empty path isn't mistaken for "no path" (review-M2)
        if engine._projection_degraded:
            out["projection_degraded"] = engine._projection_degraded
        return out

    @mcp.tool()
    @_tool_result
    def kg_explain_path(nodes: list[str]) -> dict:
        """Trace the associative chain between concepts over GROUNDED edges only (read-only egress, §2).
        Returns the ordered `path`, the grounded `edges` used (relation+span, for audit), and an ADVISORY
        `leap` = path length signalling creative distance — never a verdict, never written, never a score.
        For >2 nodes the order comes from a deterministic nearest-neighbour walk (a TSP approximation,
        byte-stable across processes) over the grounded shortest-path closure.
        EMPTY (path=[], leap=null) with a `reason` when no fully-grounded path exists — informative: the
        concepts are joined only through unverified/hypothesized/refuted links, or not at all."""
        return engine.explain_path(nodes)

    @mcp.tool()
    @_tool_result
    def kg_context(query: str | None = None, budget: int = 2000) -> dict:
        """Grounding-aware, provenance-carrying, token-budgeted context for the session."""
        return engine.kg_context(query, budget)

    @mcp.tool()
    @_tool_result
    def kg_agenda(limit: int = 5) -> dict:
        """Read-only structural "suggested questions" (R6). Reads ONLY precomputed derived columns and
        returns ~limit structural gaps split into answerable_now[] (well-grounded neighbourhoods) vs
        blocked_on_grounding[] (orphans, hypothesized-only neighbourhoods, under-grounded hubs,
        disconnected clusters). Ranked by the honest gate-aware signal (mirrors kg_context). It suggests,
        never acts — asserts no edges, copies no spans, stamps no verdicts (measure-never-gate); a
        hypothesized-only neighbourhood surfaces as BLOCKED, never as answerable. Heuristic, not a guarantee."""
        return engine.kg_agenda(limit=limit)

    @mcp.tool()
    @_tool_result
    def kg_export(kind: str = "all") -> dict:
        """Render the human-facing artifacts (R1): a self-contained, offline `graph.html` (vanilla-JS force
        layout encoding the three axes on independent channels — epistemic_state→line, authored_by→border,
        provenance→fill; size=degree; failed/rejected edges drawn, never filtered) and a `GRAPH_REPORT.md`,
        under the derived dir. `kind ∈ {html, report, all}`. READ-ONLY — projects-if-stale, consumes only the
        derived layer, writes only its two disposable artifacts; never forges a verdict or touches the canon."""
        return engine.kg_export(kind)


def _start_watchdog() -> "_Watchdog | None":
    """Construct + start the handler watchdog from KG_HANDLER_TIMEOUT (default DEFAULT_HANDLER_TIMEOUT;
    0/negative/invalid disables it), and publish it for the tool envelope to feed. Returns it (or None)."""
    global _WATCHDOG
    try:
        timeout = float(os.environ.get("KG_HANDLER_TIMEOUT", DEFAULT_HANDLER_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_HANDLER_TIMEOUT
    if timeout <= 0:
        return None
    _WATCHDOG = _Watchdog(timeout).start()
    return _WATCHDOG


def main() -> None:
    # Configure the rotating server log + uncaught-exception hooks FIRST, before anything that can fail,
    # so even an engine-construction error lands in <KG_DATA>/server.log with a full traceback (the whole
    # transport-crash class was previously undiagnosable because nothing was persisted).
    configure_logging()
    logger.info("kg_engine.server starting (version=%s pid=%s)", __version__, os.getpid())
    _start_watchdog()
    try:
        from mcp.server.fastmcp import FastMCP
        # The lifespan writes <KG_DATA>/.engine-ready as the serve loop comes up so the Node supervisor can
        # tell a post-init crash (exit clean -> client reconnects) from a startup crash (relaunch in place).
        mcp = FastMCP("creativity-graph", lifespan=readiness_lifespan)
        engine = build_engine_from_env()
        _register(mcp, engine)
        # mcp.run() returns NORMALLY on a clean client disconnect (stdin EOF closes the stdio transport
        # and the serve loop exits) -> exit 0, and the supervisor does NOT relaunch (session ending). A
        # per-request cancellation does NOT reach here: the mcp serve loop isolates each handler and keeps
        # serving (see _tool_result). Only an UNEXPECTED exception escaping the loop is a crash.
        mcp.run()
    except KeyboardInterrupt:
        logger.info("server interrupted (SIGINT); shutting down cleanly")
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — log EVERY way the serve loop can die before the process goes
        logger.critical("server crashed out of the serve loop; exiting %s so the supervisor relaunches",
                        EXIT_CRASH, exc_info=True)
        # SystemExit triggers normal interpreter shutdown (atexit -> logging flush), so no explicit
        # shutdown() here — that would close handlers before the `finally` line below is logged.
        raise SystemExit(EXIT_CRASH)
    finally:
        logger.info("server exiting (pid=%s)", os.getpid())


if __name__ == "__main__":
    main()
