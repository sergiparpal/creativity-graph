"""The reconciler (P_reconcile, §1.8).

An mtime/size pre-filter backed by a periodic full re-hash sweep (the pre-filter is for performance;
the sweep defeats mtime spoofing). On an out-of-band change it re-validates through the boundary; in
particular an out-of-band epistemic_state transition into a verdict (a forged verdict) with no matching
``kg_ground`` audit record is re-quarantined. Also runs after a derived-layer rebuild to re-attach
grounding verdicts and surface verdicts orphaned by edges that disappeared.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .canon import Canon, GROUND_AUDIT
from .groundaudit import GroundAuditLog
from .model import EpistemicState, GROUNDABLE_STATES, VERDICT_STATES, node_from_markdown, slug

__all__ = ["Reconciler", "ReconcileReport", "OrphanReport", "GROUND_AUDIT"]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


@dataclass
class ReconcileReport:
    scanned: int = 0
    changed: list[str] = field(default_factory=list)
    requarantined: list[str] = field(default_factory=list)  # edge/node ids reset from a forged verdict
    full_sweep: bool = False


@dataclass
class OrphanReport:
    reattached: int = 0
    orphaned_verdicts: list[str] = field(default_factory=list)  # verdicts whose edge vanished


class Reconciler:
    # Size of the bounded prefix-tail window hashed as an anchor for the incremental audit fold below.
    _AUDIT_ANCHOR_WINDOW = 4096

    def __init__(self, canon: Canon, state_path: str | Path | None = None):
        self.canon = canon
        self.state_path = Path(state_path) if state_path else (canon.root / ".kg-reconcile-state.json")
        self.audit_path = canon.root / GROUND_AUDIT
        # The audit log is the durable trust root; this seam appends/reads the §1.8 spend-ledger
        # checkpoint that lets a sweep RECOVER `consumed`/`epistemic` after the disposable reconcile-state
        # cache is lost (the incremental count fold below reads the bytes directly for its offset cache).
        self._ground_audit = GroundAuditLog(self.audit_path)
        # In-process incremental-fold cache for the append-only audit log (reconciler-18). The audit log
        # is read+parsed in full on every sweep and grows unbounded; folding only the NEW bytes since the
        # last sweep avoids re-parsing the whole log when scan() runs more than once in this process. This
        # cache is per-INSTANCE and deliberately NOT persisted: a fresh process reloads `consumed` from
        # the state file fresh, so binding the fold's lifetime to the process keeps it in lockstep with
        # `consumed` — a persisted-but-stale-high fold could let an already-consumed record look available
        # again (a MISSED forgery), the one direction §1.8 must never allow. The per-pair fold is a pure
        # additive count (no cross-line/global state), so an incremental fold yields counts byte-identical
        # to a full re-parse. Any inability to PROVE the cache matches the current file falls back to a
        # full re-read from offset 0 (truncation/rotation/in-place rewrite), which equals the old behavior.
        self._audit_offset = 0                       # bytes of the log already folded into the cache
        self._audit_counts_cache: dict[str, int] = {}
        self._audit_anchor = b""                     # sha256 of the prefix-tail window at _audit_offset

    # ---- state
    def _load_state(self) -> dict:
        try:
            # engine-written file: pin utf-8 so non-ASCII ids round-trip regardless of locale (§1.8).
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            # valid-JSON-but-non-dict (e.g. `[1,2,3]`, `null`) would crash scan() with AttributeError,
            # and that crash (swallowed by the background reconcile) never self-heals because scan dies
            # before _save_state. Coerce a non-dict to the fresh-dict default via the same fail-open path.
            if not isinstance(state, dict):
                raise ValueError("reconcile state is not a JSON object")
            return state
        except (FileNotFoundError, ValueError, OSError):
            return {"files": {}, "epistemic": {}, "consumed": {}}

    def _save_state(self, state: dict) -> None:
        from .canon import _atomic_write
        _atomic_write(self.state_path, json.dumps(state, indent=0))

    @staticmethod
    def _coerce_subdict(state: dict, key: str) -> dict:
        """Read a state sub-dict that scan() mutates in place: `or {}` rescues a null value, the
        isinstance guard rescues a non-dict (e.g. a list) — both fail open to a fresh dict so a
        hand-edited / truncated state can't crash scan() before _save_state heals it."""
        v = state.get(key) or {}
        return v if isinstance(v, dict) else {}

    @staticmethod
    def _anchor(blob: bytes, offset: int) -> bytes:
        """sha256 of the prefix-tail window ending at `offset` — the fingerprint that proves the cached
        prefix [0, offset) is still a valid prefix of the current file. Window size and slice semantics
        live here once so the reuse check and the re-pin can never drift. Deliberately distinct from the
        module-level `_sha256` (which takes a Path and returns a hexdigest)."""
        w = Reconciler._AUDIT_ANCHOR_WINDOW
        return hashlib.sha256(blob[max(0, offset - w):offset]).digest()

    @staticmethod
    def _fold_audit_lines(blob: bytes, counts: dict[str, int]) -> None:
        """Fold a byte blob of complete audit lines into `counts` in place (one increment per VERDICT
        record). `blob` MUST end on a record boundary (caller trims any partial trailing line). Decoded
        with `errors="replace"` so a single undefined byte only corrupts (and thus drops, via the json
        parse) its OWN line rather than blinding the reconciler to the whole log — matching the per-line
        tolerance for a corrupt JSON line. Checkpoint records (the `_ckpt` marker, §1.8 spend-ledger
        snapshots) carry no verdict pair and are skipped so they never count as a transition."""
        for line in blob.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # one corrupt audit line must not blind the reconciler to the rest
            if not isinstance(rec, dict) or "_ckpt" in rec:
                continue  # checkpoint snapshot, not a verdict transition
            pair = f"{rec.get('key', '')}||{rec.get('to', '')}"
            counts[pair] = counts.get(pair, 0) + 1

    def _audit_counts(self) -> dict[str, int]:
        """How many kg_ground audit records justify each `key -> state` transition. Counting (rather
        than set-membership) is what defeats a *replay*: each legitimate transition consumes exactly
        one record, so re-applying a previously-audited verdict out-of-band has no record left to
        justify it and is caught as a forgery.

        The log is append-only, so on a repeat sweep in THIS process we fold only the bytes appended
        since the last sweep (reconciler-18) instead of re-parsing the whole (unbounded) log. We commit
        to the cache only after the new bytes are fully folded (so a mid-fold error never leaves a
        half-advanced offset), and we PROVE the cached prefix is still valid before trusting it:
          - file shorter than the cached offset  -> truncation/rotation: full re-read from 0;
          - the prefix-tail window changed         -> in-place rewrite/rotation: full re-read from 0.
        Either fallback recomputes ground truth exactly as the old full read did, so the optimization
        can only ever lose the SPEEDUP, never miss a record (and a stale-high count is impossible: a
        shrink or rewrite forces the full recompute)."""
        try:
            # Read as BYTES so the fold offset is an exact file position (a text read would re-encode and
            # break the offset under multibyte ids). FileNotFoundError/OSError -> "no audit", fail open.
            blob = self.audit_path.read_bytes()
        except (FileNotFoundError, OSError):
            # A vanished/unreadable log resets the fold so a later recreated log re-folds from scratch.
            self._audit_offset, self._audit_counts_cache, self._audit_anchor = 0, {}, b""
            return {}

        size = len(blob)
        # Decide whether the cached prefix [0, _audit_offset) is still a valid prefix of the current
        # file. It is iff the file is at least that long AND the tail window ending at the offset is
        # byte-identical to what we hashed last time. Any mismatch -> recompute from scratch.
        reuse = (
            self._audit_offset > 0
            and size >= self._audit_offset
            and self._anchor(blob, self._audit_offset) == self._audit_anchor
        )
        if reuse:
            counts = dict(self._audit_counts_cache)
            new_start = self._audit_offset
        else:
            counts = {}
            new_start = 0

        # Fold only up to the last COMPLETE record: a trailing partial line (an append in progress, e.g.
        # a kg_ground that has written bytes but not yet the newline) must not be counted or folded into
        # the offset, so the next sweep re-reads it whole.
        nl = blob.rfind(b"\n")
        fold_end = nl + 1 if nl >= new_start else new_start
        try:
            self._fold_audit_lines(blob[new_start:fold_end], counts)
        except UnicodeError:
            # locale-mismatched / undefined bytes: degrade to "no audit" and fail open (§1.8). Leave the
            # cache untouched (do NOT advance the offset) so a later clean read can re-fold.
            return {}

        # Commit the cache ONLY after the new bytes folded cleanly: the offset advances to the boundary
        # we actually parsed, and the anchor pins the new prefix tail for the next sweep's prefix check.
        self._audit_offset = fold_end
        self._audit_counts_cache = dict(counts)
        self._audit_anchor = self._anchor(blob, fold_end)
        return counts

    @staticmethod
    def _node_keys(node) -> "list[str]":
        """The node + edge baseline keys a parsed node contributes. Single-sources the `node:` prefix
        so the incremental live-set and the all_nodes() recompute the prune relies on stay byte-identical."""
        return [f"node:{node.id}", *(e.id for e in node.edges)]

    @staticmethod
    def _file_record(st, digest: str, keys) -> dict:
        """The four-field files_state cache record for one note (mtime/size/sha256/keys), built once
        so the two construction sites in scan() can't drift if a field is added or renamed."""
        return {"mtime": st.st_mtime, "size": st.st_size, "sha256": digest, "keys": keys}

    @staticmethod
    def _file_keys(p) -> "list[str] | None":
        """The node + edge baseline keys a single canon file contributes, or None if it cannot be
        parsed — matching all_nodes()/all_edges(), which skip a malformed note. Used to maintain
        live_keys incrementally in scan() (reconciler-6) so the prune pass needn't re-read+parse the
        whole canon a SECOND time per sweep. Parses by-path with fallback_id=p.stem, byte-identically
        to all_nodes(), so the incrementally-built live set equals the old all_nodes()-derived one."""
        try:
            node = node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=p.stem)
        except Exception:  # noqa: BLE001 — a malformed note has no parseable keys
            return None
        return Reconciler._node_keys(node)

    def _requarantine_forged(self, node, epistemic: dict, consumed: dict, audit: dict[str, int],
                             report: ReconcileReport) -> bool:
        """Reset any out-of-band forged verdict on `node` and its edges to UNVERIFIED, recording the
        re-quarantined ids on `report` and writing each key's current state into the `epistemic`
        baseline. Mutates `epistemic`/`consumed` in place (the latter via _forged's record spend) and
        returns True iff anything was reset, so scan() knows the node must be re-persisted (§1.8)."""
        mutated = False

        # node-level forged verdict
        nkey = f"node:{node.id}"
        if self._forged(nkey, node.epistemic_state, epistemic, consumed, audit):
            node.epistemic_state = EpistemicState.UNVERIFIED
            report.requarantined.append(node.id)
            mutated = True
        epistemic[nkey] = node.epistemic_state.value

        # edge-level forged verdicts
        for e in node.edges:
            ekey = e.id
            if self._forged(ekey, e.epistemic_state, epistemic, consumed, audit):
                e.epistemic_state = EpistemicState.UNVERIFIED
                e.verdict_by = None
                e.verdict_at = None
                report.requarantined.append(e.id)
                mutated = True
            epistemic[ekey] = e.epistemic_state.value

        return mutated

    def _relocate_to_canonical(self, node, p: Path, rel: str, files_state: dict,
                               live_files: set) -> "tuple[Path, str] | None":
        """Return the (path, rel) the corrected node should be written at, relocating a non-canonical
        filename to its canonical slug path first; return None to signal "skip this note, retry next
        sweep" (a slug collision pre-check failed). A canonical filename passes through unchanged.

        write_one persists to the CANONICAL slug path (node_path(node.id)). When the note we
        actually read lives at a NON-canonical filename (e.g. hand-created Foo.md for id 'Foo',
        slug -> foo.md), the correction must land canonically with the stale original gone —
        else a duplicate edge in two states, and re-statting the untouched original would
        re-skip it forever (reconciler-3, self-concealing).

        Detect "non-canonical" by comparing the directory-entry name to the PURE SLUG name
        (`slug(id).md`), a plain case-sensitive string compare with NO filesystem resolution.
        node_path() calls Path.resolve(), and on Windows resolve() returns the EXISTING on-disk
        casing ('Foo.md'), which would mask the difference from the canonical 'foo.md' and skip
        this correction; note_paths() yields the real stored name ('Foo.md'). And remove the
        original BEFORE writing: on a case-insensitive filesystem (macOS/Windows) Foo.md and
        foo.md are the SAME file, so a write-then-unlink would delete the just-written note, and a
        case-preserving replace would keep the stale 'Foo.md' name anyway. Deleting first lets
        write_one create a fresh, correctly-cased foo.md; on a case-sensitive FS it just drops the
        distinct duplicate. The corrected node is in memory, so a crash in the tiny unlink->write
        window at worst drops an already-forged note (re-corrected next sweep), never a grounded one."""
        canonical_name = f"{slug(node.id)}.md"
        if p.name != canonical_name:
            try:
                # Would the canonical write collide with a DISTINCT existing note (a real id≠
                # owning slug(id).md on a case-sensitive FS)? Detect BEFORE the destructive
                # unlink: the old code unlinked first, then write_one -> _check_slug_collision
                # raised, and the UNCAUGHT ValueError aborted the ENTIRE sweep with the original
                # already deleted — a vault-wide reconcile outage plus note loss (review-M1).
                self.canon._check_slug_collision(node)
            except ValueError:
                live_files.add(rel)  # keep the original; retry next sweep, don't abort the sweep
                return None
            p.unlink(missing_ok=True)  # drop the non-canonical original before the canonical write
            files_state.pop(rel, None)
            rel = canonical_name
            p = self.canon.notes_dir / canonical_name
        return p, rel

    # ---- scan
    def scan(self, full_sweep: bool = False) -> ReconcileReport:
        state = self._load_state()
        # Coerce each sub-key defensively: a hand-edited / truncated state with `{"files": null}` (or a
        # non-dict sub-value) would otherwise crash scan() before _save_state can heal it. These stay the
        # LIVE sub-dicts mutated in place below (consumed spends records in _forged, epistemic is the
        # re-quarantine baseline) and written back at the end — _coerce_subdict must never copy them out.
        files_state = self._coerce_subdict(state, "files")
        epistemic = self._coerce_subdict(state, "epistemic")
        consumed = self._coerce_subdict(state, "consumed")
        # Durability recovery (§1.8): `consumed` (the spend ledger) and `epistemic` (the re-quarantine
        # baseline) live in the git-ignored, fail-open `.kg-reconcile-state.json` cache. If that cache is
        # lost/deleted while the append-only audit log survives, `consumed` resets to {} and every
        # historical record looks unspent again — letting a previously-grounded-then-demoted edge be
        # re-forged and accepted (the count check at _forged finds an "unconsumed" stale record). So when
        # the cache carries no spend ledger, recover both from the last checkpoint persisted in the audit
        # log (the durable trust root). Merge safe-HIGH for consumed (max per pair — over-strict, never a
        # missed forgery) and adopt the checkpoint's epistemic baseline only where the cache lacks one. A
        # genuine first-ever run has no checkpoint -> stays empty, behaviour unchanged.
        if not consumed:
            recovered = self._ground_audit.last_checkpoint()
            if recovered:
                for pair, c in recovered["consumed"].items():
                    if isinstance(c, int) and c > consumed.get(pair, 0):
                        consumed[pair] = c
                for k, v in recovered["epistemic"].items():
                    epistemic.setdefault(k, v)
        audit = self._audit_counts()
        report = ReconcileReport(full_sweep=full_sweep)
        # On a FULL SWEEP we visit and hash every note in the loop below, so we can build the live
        # file/key sets INCREMENTALLY (reconciler-6) and skip the old post-loop all_nodes() re-parse —
        # a hash match proves the cached keys are current, so the incremental sets are byte-identical to
        # the all_nodes()-derived ones. On the cheap (mtime/size) pre-gate path the loop SKIPS unchanged
        # notes, so cached keys could be stale under an (mtime,size) collision; there we fall back to the
        # authoritative all_nodes() recompute for pruning, exactly as before this change.
        live_files: set[str] = set()
        live_keys: set[str] = set()

        for p in self.canon.note_paths():  # excludes .tmp-* atomic-write temporaries (canon-5)
            report.scanned += 1
            rel = p.name
            # scan() holds no lease, so a lease-holding writer (kg_rename's unlink, an atomic
            # temp+replace) can delete/rename a note BETWEEN note_paths()'s snapshot and these
            # stat/hash syscalls. An unguarded raise here aborts the WHOLE §1.8 sweep before
            # _save_state, silently disabling forge-detection for the session (reconciler-7). Skip a
            # vanished/transiently-unreadable note instead: it is correctly absent from live_files (and
            # pruned if truly gone), and re-examined next sweep if it still exists.
            try:
                st = p.stat()
            except OSError:
                continue  # deleted/renamed concurrently; not live this sweep
            prev = files_state.get(rel, {})
            # pre-filter: unchanged mtime+size and not a full sweep -> skip the expensive re-read
            prefilter_same = (prev.get("mtime") == st.st_mtime and prev.get("size") == st.st_size)
            if prefilter_same and not full_sweep:
                continue  # cheap pre-gate: trust (mtime,size); the full-sweep prune below re-reads
            try:
                digest = _sha256(p)
            except OSError:
                continue  # vanished/unreadable between stat and hash; retry next sweep
            if full_sweep and prefilter_same and prev.get("sha256") == digest:
                # mtime/size AND hash matched -> genuinely unchanged even under sweep. Carry the cached
                # keys forward (backfill once if absent) so live_keys stays complete without a re-parse.
                keys = prev.get("keys")
                if keys is None:
                    keys = self._file_keys(p)
                files_state[rel] = self._file_record(st, digest, keys if keys is not None else [])
                live_files.add(rel)
                if keys:
                    live_keys.update(keys)
                continue

            report.changed.append(rel)
            try:
                # parse the file ON DISK directly, not via read_node(p.stem) which re-slugs the stem
                # and could resolve to a different/missing file for a non-slug-canonical filename
                # (reconciler-4) — leaving that note un-reconciled on every scan.
                node = node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=p.stem)
            except Exception:  # noqa: BLE001 — a single malformed note must not crash the sweep
                # the note still exists on disk (so its files_state entry must survive the prune), but a
                # malformed note contributes NO parseable keys — exactly what all_nodes() would yield.
                live_files.add(rel)
                continue  # leave its file_state untouched so it's retried next scan

            # A re-quarantine is a read->mutate->write; the reconciler holds no lease across it, so a
            # concurrent kg_ground (separate process) could ground a SIBLING edge on this same node
            # between our read and our write and our stale in-memory copy would clobber that verdict
            # (lost update, reconciler-M2). Take the lease for the critical section and decide on a FRESH
            # read under it — mirroring kg_ground's read-under-lease discipline — so we reset only what
            # is STILL forged and never drop a concurrently-applied sibling verdict. If the lease can't
            # be taken (another live writer), fall back to the snapshot read: correctness is unchanged
            # (any clobbered verdict is re-applied by its own kg_ground; this note retries next sweep),
            # and single-process callers (the lease is free) always take the fast lease path.
            locked = self.canon.try_acquire_lock()
            try:
                if locked:
                    try:
                        node = node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=p.stem)
                    except OSError:
                        live_files.add(rel)  # vanished/unreadable under the lease; retry next sweep
                        continue
                    except Exception:  # noqa: BLE001 — became malformed; no parseable keys, retry next
                        live_files.add(rel)
                        continue
                mutated = self._requarantine_forged(node, epistemic, consumed, audit, report)

                if mutated:
                    relocated = self._relocate_to_canonical(node, p, rel, files_state, live_files)
                    if relocated is None:
                        continue  # collision pre-check failed: keep the original, retry next sweep
                    p, rel = relocated
                    try:
                        self.canon.write_one(node)
                    except Exception:  # noqa: BLE001 — one unwritable/colliding note must not abort sweep
                        live_files.add(rel)
                        continue
                    try:
                        st = p.stat()
                        digest = _sha256(p)
                    except OSError:
                        live_files.add(rel)  # vanished right after our write; cache it next sweep
                        continue
            finally:
                if locked:
                    self.canon._release_lock()
            keys = self._node_keys(node)
            files_state[rel] = self._file_record(st, digest, keys)
            live_files.add(rel)
            live_keys.update(keys)

        # Prune the `epistemic` BASELINE for anything no longer live. Edge ids are deterministic, so a
        # deleted-then-recreated edge would otherwise inherit the old "already grounded" baseline and
        # let a forged verdict slip past the `last == current` short-circuit in _forged (reconciler-1).
        # Dropping dead keys also bounds growth across renames/churn (reconciler-2, whose comment
        # previously claimed a pruning that did not happen).
        #
        # `consumed` is deliberately NOT pruned: it is the running tally of audit records already
        # spent, and the audit log is append-only (never pruned). Pruning consumed would let an old,
        # still-present audit record be re-spent by a recreated edge — re-opening the very replay the
        # count check defeats. Its growth tracks the audit log, which is the permanent record anyway.
        if full_sweep:
            # every note was visited+hashed in the loop, so the incremental sets equal an
            # all_nodes()/note_paths() recompute exactly — without the redundant second full read.
            prune_files, prune_keys = live_files, live_keys
        else:
            # cheap pre-gate skipped unchanged notes (their cached keys may be stale under an (mtime,size)
            # collision), so recompute authoritatively — byte-identical to the pre-reconciler-6 prune.
            # (Trusting cached keys here could leave a stale baseline key and reopen a delete→recreate→
            # forge bypass — see test_f29_cheap_path_uses_authoritative_all_nodes — so it stays
            # deliberately authoritative.)
            live_nodes = self.canon.all_nodes()
            prune_files = {p.name for p in self.canon.note_paths()}
            prune_keys = {f"node:{n.id}" for n in live_nodes} | {e.id for n in live_nodes for e in n.edges}
        for rel in [r for r in files_state if r not in prune_files]:
            del files_state[rel]
        for k in [k for k in epistemic if k not in prune_keys]:
            del epistemic[k]

        self._save_state({"files": files_state, "epistemic": epistemic, "consumed": consumed})

        # Durably checkpoint the spend ledger into the append-only audit log (the trust root) so a later
        # loss of the disposable reconcile-state cache can RECOVER it (above) instead of replaying every
        # historical record (§1.8). A full sweep is a consistent snapshot of every live key, so checkpoint
        # there; scan() runs full_sweep per session, keeping growth to ~one record per session. Gated on
        # the lease (the audit log is lease-guarded like every writer) and best-effort: a busy lease or a
        # failed append just defers to the next sweep, and a stale checkpoint is only ever safe-high.
        if full_sweep and self.canon.try_acquire_lock():
            try:
                self._ground_audit.append_checkpoint(consumed, epistemic)
            except Exception:  # noqa: BLE001 — checkpointing must never break the sweep
                pass
            finally:
                self.canon._release_lock()

        # Housekeeping (full sweep only): reap bounded-retention transient dotfiles (old `.bak` self-heal
        # backups beyond the retention cap, crash-leftover `.tmp-*`, sidelined lock records) that would
        # otherwise accumulate unbounded in the canon dir (canon-reaper). Best-effort: reap_transient_files
        # keeps the newest N backups per note, TTL-gates the .tmp-*/sideline removal so it never races a
        # live atomic write/reclaim, and swallows its own OSErrors — so it can never abort the §1.8 sweep.
        if full_sweep:
            try:
                self.canon.reap_transient_files()
            except Exception:  # noqa: BLE001 — housekeeping must never break the sweep
                pass
        return report

    @staticmethod
    def _drain_key_ledger(key: str, consumed: dict, audit: dict[str, int]) -> None:
        """Mark EVERY audit record for `key` as consumed (consumed[pair] = audit[pair] for each of the
        key's pairs). Called once `key`'s current canon state is validated as legitimate: that state is
        authoritative, so all historical records that led here are spent and none may justify a FUTURE
        out-of-band transition. Pair keys are `f"{key}||{state}"`; isolate this key's pairs by splitting
        the trailing `||{state}` off the LAST `||` (the state value never contains `||`) rather than a
        prefix match — a hand-edited node id that itself contains `||` (raw frontmatter ids are not
        re-slugged on read, model.node_from_markdown) would otherwise let `node:a` over-drain `node:a||b`'s
        ledger and wrongly re-quarantine a sibling's legit verdict (reconciler-||collision)."""
        for pair, count in audit.items():
            if pair.rsplit("||", 1)[0] == key and count > consumed.get(pair, 0):
                consumed[pair] = count

    def _forged(self, key: str, current: EpistemicState, epistemic: dict, consumed: dict,
                audit: dict[str, int]) -> bool:
        """True if `current` is a policed state reached out-of-band: it differs from the last validated
        state for this key and there is no UNCONSUMED kg_ground audit record justifying a transition
        into `current`. Each legitimate transition consumes one audit record, so replaying a stale
        verdict (whose record was already spent) is caught. The policed set is GROUNDABLE_STATES — the
        verdicts PLUS `obsolete` — the same set kg_ground stamps and audits; `obsolete` was previously
        excluded, so an out-of-band edit to `obsolete` (which the write boundary demotes) silently
        survived the sweep and could erase a grounding verdict / failure memory (reconciler-5/server-1)."""
        if current not in GROUNDABLE_STATES:
            return False
        last = epistemic.get(key)
        pair = f"{key}||{current.value}"
        if last == current.value:
            # Unchanged since last validated — nothing new to justify, and the current state is
            # authoritative, so drain the WHOLE ledger for this key into `consumed`. An idempotent
            # re-ground appends a fresh record on every call (reconciler-H1), AND an edge that passed
            # through an intermediate policed state before settling here leaves records for those states
            # too — any such surplus is a record a later out-of-band forgery back into that state could
            # spend and slip past undetected. Draining ALL of the key's pairs (not just `pair`) leaves no
            # spendable record behind.
            self._drain_key_ledger(key, consumed, audit)
            return False
        if audit.get(pair, 0) > consumed.get(pair, 0):
            # A legitimate transition: an unconsumed record justifies reaching `current`. The current
            # state is now authoritative, so drain the ENTIRE ledger for this key — INCLUDING records for
            # intermediate states the sweep never observed-as-current (e.g. a `grounded` record on an edge
            # that went unverified->grounded->failed within a prior session). Spending only ONE record for
            # `pair` (the old behavior) left every such intermediate record as a permanent spendable
            # surplus a later out-of-band forgery into that state could replay to evade re-quarantine.
            self._drain_key_ledger(key, consumed, audit)
            return False
        # The snapshot `audit` (captured once at the top of the sweep) shows no record — but the
        # per-session reconcile runs in a SEPARATE process concurrently with kg_ground, so a LEGITIMATE
        # verdict (record appended + canon write) can land AFTER the snapshot yet before this note's
        # read. Before declaring a forgery (which would permanently revert that just-applied verdict),
        # re-fold the audit log FRESH to pick up any record appended mid-sweep; honor it if found
        # (reconciler-H3/M2). Adopt ALL of this key's fresh pair counts into the working snapshot so the
        # subsequent ledger drain also spends any mid-sweep intermediate records.
        fresh = self._audit_counts()
        if fresh.get(pair, 0) > audit.get(pair, 0):
            for p, c in fresh.items():
                if p.rsplit("||", 1)[0] == key and c > audit.get(p, 0):
                    audit[p] = c  # adopt mid-sweep records for this key into the working snapshot
            if audit.get(pair, 0) > consumed.get(pair, 0):
                self._drain_key_ledger(key, consumed, audit)
                return False
        return True

    # ---- post-reproject reattachment
    def reattach_after_reproject(self, graph_json: str | Path) -> OrphanReport:
        report = OrphanReport()
        try:
            # engine-written derived layer: pin utf-8 so non-ASCII edge ids match the canon (parsed
            # utf-8) regardless of locale, rather than mojibake-missing a live verdict (§1.8).
            data = json.loads(Path(graph_json).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return report
        derived_edge_ids = {e.get("id") for e in data.get("links", data.get("edges", []))}
        for e in self.canon.all_edges():
            # only true verdicts (grounded/rejected/failed) are "verdicts" that can be orphaned;
            # OBSOLETE is a lifecycle state, not a verdict, so it is not reported here.
            if e.epistemic_state in VERDICT_STATES:
                if e.id in derived_edge_ids:
                    report.reattached += 1
                else:
                    report.orphaned_verdicts.append(e.id)
        return report
