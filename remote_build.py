#!/usr/bin/env python3
"""Temporary remote build client.

Usage (from any Rust / Go / Node.js project)::

    export REMOTE_BUILD_REPO=OWNER/remote-rust-build
    python remote_build.py
    python remote_build.py --command "cargo check"
    python remote_build.py --command "go build -o dist/app ./..."
    python remote_build.py --command "npm ci && npm run build"

Flow: archive current files -> temp branch in the dedicated build repo ->
workflow_dispatch build.yml -> wait -> print compiler log -> download
binary -> delete run/branch/local temp files.  Never commits in your
project; the temporary commit lives only in the build repo.
Standard library only; GitHub auth is delegated to the `gh` CLI.
"""

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path

DEFAULT_COMMAND = "cargo build --release"
DEFAULT_REPO = "developic/remote-build"
WORKFLOW_FILE = "build.yml"
ARTIFACT_PREFIX = "remote-build-binary"
PROTECTED_BRANCHES = {"main", "master", "HEAD"}

# Directories / files never uploaded (plus credential patterns below).
EXCLUDE_DIRS = {".git", "target", "node_modules", "dist", "build",
                "__pycache__", ".venv", "venv"}
EXCLUDE_FILES = {"remote-build-output", ".env"}
CREDENTIAL_PATTERNS = ("*.key", "*.pem", "*.p12", "*.pfx", "*.asc",
                       "*.gpg", "*id_rsa*", "*id_ed25519*",
                       "credentials*.json", "*secret*", ".env*")


_QUIET = False  # quiet by default: live one-line progress, errors only.
_PULSE_STOP = threading.Event()
_PULSE_THREAD = None
_SPIN_I = 0
_SPINNER = "|/-\\"

# ANSI styling for the live display (tty only, stdlib-only, no deps).
_C = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
}


def _use_style():
    return (sys.stdout.isatty()
            and os.environ.get("NO_COLOR", "") == "")


def _live():
    """True when the one-line live animation should render (a real TTY).
    Piped/CI output falls back to plain lines so logs stay readable."""
    return _QUIET and sys.stdout.isatty()


def _term_width():
    try:
        return max(40, shutil.get_terminal_width().columns - 1)
    except Exception:
        return 100


def _write_live(text):
    """Rewrite the single live status line cleanly.

    Uses erase-to-end-of-line on styled terminals (no trailing-space
    residue, no wrap on narrow screens); space-padding fallback otherwise.
    """
    width = _term_width()
    text = text[:width]
    if _use_style():
        sys.stdout.write("\r\033[K" + text)
    else:
        sys.stdout.write("\r" + text.ljust(width)[:width])
    sys.stdout.flush()


def _clear_line():
    if sys.stdout.isatty():
        if _use_style():
            sys.stdout.write("\r\033[K")
        else:
            sys.stdout.write("\r" + " " * _term_width() + "\r")
        sys.stdout.flush()


def _spin_frame():
    global _SPIN_I
    _SPIN_I += 1
    return _SPINNER[_SPIN_I % len(_SPINNER)]


def log(msg):
    """Progress info: full lines in verbose mode, live one-liner when quiet."""
    if _live():
        text = str(msg)[:100]
        if _use_style():
            _write_live(f"{_C['cyan']}{_spin_frame()}{_C['reset']} "
                        f"{_C['dim']}{text}{_C['reset']}")
        else:
            _write_live(f"* {text}")
    else:
        print(f"[remote-build] {msg}", flush=True)


def say(msg):
    """Always print a full line (clears the live progress line first)."""
    if _live():
        _clear_line()
    text = f"[remote-build] {msg}"
    if _use_style():
        if "SUCCEEDED" in msg:
            text = f"{_C['bold']}{_C['green']}[OK] {text}{_C['reset']}"
        elif msg.startswith("run: https://"):
            text = f"{_C['cyan']}{text}{_C['reset']}"
    print(text, flush=True)


def err(msg):
    if _live():
        _clear_line()
    text = f"[remote-build] ERROR: {msg}"
    if _use_style() or (sys.stderr.isatty()
                        and os.environ.get("NO_COLOR", "") == ""):
        text = f"{_C['bold']}{_C['red']}[FAIL] {text}{_C['reset']}"
    print(text, file=sys.stderr, flush=True)


def pulse_start(label):
    """Live elapsed-time ticker on one line while a long step blocks."""
    global _PULSE_THREAD
    if not _live():
        return
    _PULSE_STOP.clear()

    def tick():
        start = time.time()
        i = 0
        while not _PULSE_STOP.wait(1.0):
            el = int(time.time() - start)
            dots = "." * (1 + i % 3)
            if _use_style():
                _write_live(f"{_C['cyan']}{_SPINNER[i % len(_SPINNER)]}"
                            f"{_C['reset']} {label}{dots.ljust(3)} "
                            f"{_C['yellow']}({el // 60}m{el % 60:02d}s)"
                            f"{_C['reset']}")
            else:
                _write_live(f"{_SPINNER[i % len(_SPINNER)]} {label} "
                            f"({el // 60}m{el % 60:02d}s)")
            i += 1

    _PULSE_THREAD = threading.Thread(target=tick, daemon=True)
    _PULSE_THREAD.start()


def pulse_stop():
    global _PULSE_THREAD
    if _PULSE_THREAD is not None:
        _PULSE_STOP.set()
        _PULSE_THREAD.join(timeout=2)
        _PULSE_THREAD = None
    if _live():
        _clear_line()


def run(cmd, cwd=".", check=True, capture=True, env=None, timeout=None):
    """Run a subprocess; raise RuntimeError with stderr on failure."""
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=capture, text=True,
        env=env, timeout=timeout,
    )
    if check and result.returncode != 0:
        out = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{out}")
    return result


def check_tool(name):
    if shutil.which(name) is None:
        err(f"'{name}' not found in PATH.")
        return False
    return True


def is_credential_path(rel_posix):
    base = os.path.basename(rel_posix)
    for pat in CREDENTIAL_PATTERNS:
        if fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(rel_posix, pat):
            return True
    return False


def list_files_git(project_root):
    """Respect .gitignore exactly; None if git unavailable/failed."""
    r = run(["git", "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            cwd=project_root, check=False)
    if r.returncode != 0:
        return None
    files = []
    for entry in r.stdout.split("\0"):
        if not entry:
            continue
        p = project_root / entry
        if p.is_file():
            files.append(p)
    return files


def list_files_walk(project_root):
    collected = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames
                       if d not in EXCLUDE_DIRS and d != ".git"]
        for fn in filenames:
            if fn in EXCLUDE_FILES:
                continue
            full = Path(dirpath) / fn
            rel = full.relative_to(project_root).as_posix()
            if is_credential_path(rel):
                continue
            if rel.startswith("remote-build-output/"):
                continue
            collected.append(full)
    return collected


def create_archive(project_root, archive_path):
    """Create temp .tar.gz of current files. Returns member count."""
    if list_files_git(project_root) is not None and \
            (project_root / ".git").exists():
        files = list_files_git(project_root) or []
        log(f"git ls-files found {len(files)} file(s).")
    else:
        files = list_files_walk(project_root)
        log(f"directory scan found {len(files)} file(s).")

    filtered = []
    for f in files:
        rel = f.relative_to(project_root).as_posix()
        parts = rel.split("/")
        if any(p in EXCLUDE_DIRS for p in parts):
            continue
        if parts[0] in EXCLUDE_FILES:
            continue
        if is_credential_path(rel):
            log(f"excluded credential file: {rel}")
            continue
        if f.resolve() == archive_path.resolve():
            continue
        filtered.append(f)

    manifests = ("Cargo.toml", "go.mod", "package.json", "Makefile")
    if not any((project_root / m).is_file() or
               any(f.name == m for f in filtered) for m in manifests):
        log("WARNING: no Cargo.toml/go.mod/package.json/Makefile found; "
            "continuing anyway.")
    if not filtered:
        raise RuntimeError("nothing to archive (all files excluded?)")

    with tarfile.open(archive_path, "w:gz") as tf:
        for f in sorted(filtered):
            tf.add(f, arcname=f.relative_to(project_root).as_posix())
    size = archive_path.stat().st_size
    log(f"archive: {archive_path} ({len(filtered)} files, {size} bytes).")
    top = sorted({p.relative_to(project_root).as_posix().split("/")[0]
                  for p in filtered})
    log(f"archive top-level entries: {', '.join(top[:25])}"
        + (" ..." if len(top) > 25 else ""))
    return len(filtered)


def safe_extract(archive_path, dest_dir):
    with tarfile.open(archive_path, "r:gz") as tf:
        for member in tf.getmembers():
            name = member.name
            if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                raise RuntimeError(f"unsafe tar member: {name}")
        tf.extractall(dest_dir)


def gh(*args, cwd=".", check=True, timeout=60):
    return run(["gh", *args], cwd=cwd, check=check, timeout=timeout)


# command prefix -> manifest that must exist at project/ root.
# Catches the classic mistake of running from the parent directory
# (manifest ends up nested, e.g. project/rust/Cargo.toml, and the
# remote `cd project && cargo ...` fails with "could not find Cargo.toml").
COMMAND_MANIFESTS = (
    ("cargo", "Cargo.toml"),
    ("go ", "go.mod"),
    ("npm", "package.json"),
    ("yarn", "package.json"),
    ("pnpm", "package.json"),
    ("make", "Makefile"),
    ("cmake", "CMakeLists.txt"),
)


def check_staged_project(proj, command):
    """Fail fast (before pushing) if project/ cannot satisfy the command."""
    entries = [p for p in proj.iterdir()]
    if not entries:
        raise RuntimeError(
            "staged project/ is empty -- nothing would be built. "
            "Are you running from the project root?")
    for prefix, manifest in COMMAND_MANIFESTS:
        if command.strip().startswith(prefix):
            if not (proj / manifest).is_file():
                nested = sorted(str(p.relative_to(proj).as_posix())
                                for p in proj.rglob(manifest))
                hint = (f" Found nested at: {', '.join(nested[:3])}."
                        if nested else "")
                raise RuntimeError(
                    f"command starts with {prefix!r} but "
                    f"project/{manifest} is missing.{hint} "
                    f"Run from the directory containing {manifest}, "
                    f"not its parent.")
            break


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Same escapes, but transported as literal caret sequences (some
# `gh`/pager paths render ESC as "^[").
CARET_ESC_RE = re.compile(r"\^\[(?:\[[0-9;]*m|\(B|>)")
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s")
# Runner plumbing hidden by the clean log (kept with --raw-log).
NOISE_RES = (
    re.compile(r"Temporarily overriding HOME="),
    re.compile(r"Adding repository directory to the .* git .*config"),
    re.compile(r"\[command\]/usr/bin/git (version|config|submodule)"),
    re.compile(r"\[command\]"),
    re.compile(r"^hint:"),
    re.compile(r"^(Syncing repository|Deleting the contents|Prepare all required|"
               r"Getting action download|Download action repository|"
               r"Complete job name|Prepare workflow directory)"),
    re.compile(r"git version \d"),
    re.compile(r"^Post job cleanup\.?$"),
    re.compile(r"^Cleaning up orphan processes\.?$"),
    re.compile(r"Node\.js \d+ is deprecated"),
    re.compile(r"^For more information see: https://github\.blog/changelog"),
    re.compile(r"^shell: /usr/bin/bash"),
    re.compile(r"actions/checkout@v4\. For more information"),
)
SKIP_STEPS = {"Post Check out temporary build branch", "Complete job",
              "Set up job", "Set up runner"}


def clean_log(raw):
    """Turn `gh run view --log` into a readable compiler log.

    Groups lines under `### <step>` headers, strips ANSI colour codes
    and timestamps, drops runner plumbing (git safe.directory setup,
    post-job cleanup, Node deprecation notices). Returns one string.
    """
    out = []
    current_step = None

    def scrub(text):
        return CARET_ESC_RE.sub("",
                                ANSI_RE.sub("", text).replace("﻿", ""))
    for raw_line in (raw or "").splitlines():
        line = scrub(raw_line)
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 3:
                _job, step, msg = parts[0], parts[1], "\t".join(parts[2:])
                msg = STAMP_RE.sub("", msg)
                if step in SKIP_STEPS:
                    continue
                if any(rx.search(msg) for rx in NOISE_RES):
                    continue
                if not msg.strip():
                    continue
                if step != current_step:
                    out.append(f"\n### {step}")
                    current_step = step
                out.append(msg)
                continue
        msg = STAMP_RE.sub("", line).rstrip()
        if not msg.strip() or any(rx.search(msg) for rx in NOISE_RES):
            continue
        out.append(msg)
    return "\n".join(out).strip() + "\n"


def error_section(cleaned, max_lines=80):
    """The failing step's output only (quiet mode): prefers the section
    holding the executed build command / compiler errors, else the tail."""
    sections, cur = [], []
    for line in (cleaned or "").splitlines():
        if line.startswith("### "):
            if cur:
                sections.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        sections.append(cur)

    def score(sec):
        text = "\n".join(sec)
        hits = sum(1 for kw in ("error", "Error", "ERROR", "FAILED",
                                "failed", "panic", "mismatched")
                   if kw in text)
        if sec and "Build" in sec[0]:
            hits += 2
        return hits

    if not sections:
        body = []
    else:
        best = max(sections, key=score)
        body = best if score(best) > 0 else sections[-1]
    body = [l for l in body if l.strip()]
    if len(body) > max_lines:
        body = (["... (truncated; re-run with --raw-log for the full log)"]
                + body[-max_lines:])
    return "\n".join(body).strip() + "\n"


def flatten_output_dir(out_dir, before):
    """Collapse download-created subfolders so the binary always lands
    directly in out_dir, replacing the previous build's files.

    `gh run download` (without -n) extracts each artifact into its own
    `<artifact-name>/` subfolder, and the artifact name contains the
    unique build ID -- without flattening, every build would add a new
    per-build folder. Only folders created by the download (absent from
    the `before` snapshot) are collapsed; anything else is left alone.
    """
    for sub in sorted(p for p in out_dir.iterdir() if p.is_dir()):
        if sub.name in before:
            continue
        for item in sorted(sub.rglob("*")):
            if not item.is_file():
                continue
            dest = out_dir / item.relative_to(sub)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            item.rename(dest)
        shutil.rmtree(sub, ignore_errors=True)


def cache_key_for(project_root, command):
    """Deterministic shared-cache key, mirroring the workflow's resolve
    step: remote-build-cache-<slug>-<lock16>-<cmd12>.

    Slug = stable project identity (manifest name), lock = deps,
    cmd = build profile. Must be identical across rebuilds of the same
    project -- never the per-build ID.
    """
    name = ""
    cargo = project_root / "Cargo.toml"
    gomod = project_root / "go.mod"
    pkg = project_root / "package.json"
    try:
        if cargo.is_file():
            m = re.search(r"(?m)^[ \t]*name[ \t]*=[ \t]*\"([^\"]+)\"",
                          cargo.read_text(errors="replace"))
            name = "rust-" + (m.group(1) if m else "unknown")
        elif gomod.is_file():
            m = re.search(r"(?m)^module[ \t]+([^ \t]+)",
                          gomod.read_text(errors="replace"))
            mod = m.group(1) if m else "unknown"
            name = "go-" + mod.rsplit("/", 1)[-1]
        elif pkg.is_file():
            m = re.search(r'(?m)^[ \t]*"name"[ \t]*:[ \t]*"([^"]+)"',
                          pkg.read_text(errors="replace"))
            mod = m.group(1) if m else "unknown"
            name = "node-" + mod.rsplit("/", 1)[-1]
    except OSError:
        pass
    if not name:
        name = "unknown"
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:48].strip("-") or "unknown"
    h = hashlib.sha256()
    found = False
    for lock in ("Cargo.lock", "go.sum", "package-lock.json"):
        p = project_root / lock
        try:
            if p.is_file():
                h.update(p.read_bytes())
                found = True
        except OSError:
            pass
    lock16 = h.hexdigest()[:16] if found else "nolock"
    cmd12 = hashlib.sha256(command.encode()).hexdigest()[:12]
    key = f"remote-build-cache-{slug}-{lock16}-{cmd12}"
    return slug, lock16, cmd12, key, f"remote-build-cache-{slug}-"


def purge_cache_payload(out_dir):
    """Remove downloaded `cache-payload-*` entries (dependency tarballs
    for the seeder, not build outputs). They arrive only via the
    download-all fallback; the binary output must never contain them."""
    for p in sorted(out_dir.iterdir()):
        if p.name.startswith("cache-payload-"):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    p.unlink()
                except OSError:
                    pass
            log(f"discarded cache payload: {p.name}")


def download_artifact_zip(repo, artifact_id, artifact_name, out_dir,
                          timeout=600, retries=3):
    """Download one artifact by its API id (the same zip the Actions UI
    links to: /actions/runs/<run>/artifacts/<id>) and extract it with
    the standard library. Retries transient network failures. Returns
    True on success. Never touches any other artifact.
    """
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(
                ["gh", "api",
                 f"repos/{repo}/actions/artifacts/{artifact_id}/zip",
                 "--repo", repo],
                capture_output=True, timeout=timeout)
            if proc.returncode != 0:
                last_err = proc.stderr.decode(
                    errors="replace").strip() or "gh api failed"
                raise RuntimeError(last_err)
            fd, tmppath = tempfile.mkstemp(prefix="remote-build-dl-",
                                          suffix=".zip")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(proc.stdout)
                with zipfile.ZipFile(tmppath) as zf:
                    for member in zf.namelist():
                        parts = Path(member).parts
                        if member.startswith(("/", "\\")) or \
                                ".." in parts:
                            raise RuntimeError(
                                f"unsafe zip member: {member}")
                    zf.extractall(out_dir)
            finally:
                try:
                    os.unlink(tmppath)
                except OSError:
                    pass
            return True
        except (RuntimeError, subprocess.TimeoutExpired,
                zipfile.BadZipFile, OSError) as e:
            last_err = str(e)
            log(f"artifact download attempt {attempt}/{retries} failed "
                f"({artifact_name}): {last_err}")
            time.sleep(5 * attempt)
    err(f"artifact download failed (nothing uploaded?). Last error: "
        f"{last_err}")
    return False


def wait_for_binary_artifact(repo, run_id, artifact, timeout_s=300):
    """Poll the run's artifact list until the binary artifact appears.

    The listing can lag seconds behind run completion; without waiting,
    a strict downloader mistakes a slow API for a missing artifact.
    Returns (kind, artifact_id, name) where kind is one of:
      exact        -- our artifact (id usable for direct download)
      legacy       -- older naming (fixed name or another build id)
      payload-only -- success with no binary (e.g. `cargo check`)
      empty        -- no artifacts at all
      unknown      -- API unreachable; caller decides
    """
    deadline = time.time() + timeout_s
    last = ("unknown", None, None)
    fallback = None
    while time.time() < deadline:
        try:
            r = gh("api", f"repos/{repo}/actions/runs/{run_id}/artifacts",
                   "--jq", ".artifacts[] | "
                           "\\(.id) \\(.name) expired=\\(.expired)",
                   timeout=120)
            entries = []
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and "expired=true" not in line:
                    entries.append((parts[0], parts[1]))
        except RuntimeError:
            time.sleep(10)
            continue
        names = {n for _, n in entries}
        if artifact in names:
            aid = next(a for a, n in entries if n == artifact)
            return ("exact", aid, artifact)
        # Remember legacy names but keep waiting: the exact artifact
        # may simply be listed a few seconds later.
        if fallback is None:
            legacy = sorted({n for n in names
                             if n == "remote-build-binary"
                             or n.startswith("remote-build-binary-")})
            if legacy:
                aid = next(a for a, n in entries if n == legacy[0])
                fallback = ("legacy", aid, legacy[0])
                log(f"exact artifact name not found yet; will use "
                    f"{legacy[0]} if it never appears...")
        if any(n.startswith("cache-payload-") for n in names):
            last = ("payload-only", None, None)
        elif not names:
            last = ("empty", None, None)
        else:
            last = ("unknown", None, None)
        time.sleep(10)
    return fallback or last


def poll_run_to_completion(repo, run_id, run_url, timeout_mins=45):
    """Wait for a run, showing its REAL phase instead of a blind spinner.

    Polls the run status every 15s (queued -> in_progress -> completed),
    re-labels the live ticker on phase changes, and prints a full
    heartbeat line every 60s so even scrollback-only terminals prove
    the client is alive. Returns the conclusion ("" on timeout).
    """
    deadline = time.time() + timeout_mins * 60
    started = time.time()
    phase = ""
    last_beat = 0.0
    pulse_start("waiting for runner")
    try:
        while time.time() < deadline:
            try:
                r = gh("api",
                       f"repos/{repo}/actions/runs/{run_id}",
                       "--jq", "\\(.status) \\(.conclusion)",
                       timeout=60)
                status, concl = (r.stdout.split() + ["", ""])[:2]
            except RuntimeError:
                status, concl = "", ""
            if status and status != phase:
                phase = status
                label = ("queued on GitHub-hosted runner"
                         if phase == "queued"
                         else "building on GitHub Actions"
                         if phase == "in_progress"
                         else f"run {phase}")
                pulse_stop()
                pulse_start(label)
            now = time.time()
            if now - last_beat >= 60:
                last_beat = now
                el = int(now - started)
                say(f"still waiting... phase={phase or '?'} "
                    f"elapsed={el // 60}m{el % 60:02d}s ({run_url})")
            if status == "completed":
                return concl or ""
            time.sleep(15)
    finally:
        pulse_stop()
    return ""


def list_branch_runs(repo, branch, limit=10, workflow=WORKFLOW_FILE):
    """All run IDs on a branch (dispatch + push siblings). Never raises."""
    try:
        r = gh("run", "list", "--workflow", workflow,
               "--branch", branch, "--limit", str(limit),
               "--json", "databaseId",
               "--repo", repo, check=False)
        return [str(x["databaseId"])
                for x in (json.loads(r.stdout or "[]"))]
    except Exception:
        return []


def parse_repo(repo):
    if "/" not in repo or repo.count("/") != 1:
        raise RuntimeError(
            f"invalid REMOTE_BUILD_REPO={repo!r}, expected OWNER/REPO")
    owner, name = repo.split("/")
    if not owner or not name or " " in repo:
        raise RuntimeError(f"invalid REMOTE_BUILD_REPO={repo!r}")


def generate_build_id():
    """Unique per-build suffix. The temp branch is remote-build/<suffix>
    (one run per branch, triggered via workflow_dispatch); the
    build ID (no slash, safe for artifact names) is remote-build-<suffix>.
    Returns (build_id, branch)."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    rnd = f"{random.getrandbits(32):08x}"
    suffix = f"{ts}-{rnd}"
    return f"remote-build-{suffix}", f"remote-build/{suffix}"


class Cleanup:
    """Tracks everything that must be removed, even on Ctrl+C."""

    def __init__(self, repo):
        self.repo = repo
        self.archive = None
        self.clone_dir = None
        self.branch = None
        self.run_id = None
        self.run_deleted = False
        self.branch_deleted = False
        self.extra_runs = []  # e.g. the main-scope seeder run

    def cleanup(self, cancel_first=False):
        # Collect every run on the temp branch (normally exactly one)
        # plus any explicitly tracked runs (e.g. the seeder on main),
        # so nothing is left behind.
        ids = []
        if self.run_id:
            ids.append(str(self.run_id))
        for rid in self.extra_runs:
            if str(rid) not in ids:
                ids.append(str(rid))
        if self.branch and self.branch not in PROTECTED_BRANCHES:
            for rid in list_branch_runs(self.repo, self.branch):
                if rid not in ids:
                    ids.append(rid)
        for rid in ids:
            if cancel_first:
                try:
                    log(f"cancelling run {rid}...")
                    gh("run", "cancel", rid,
                       "--repo", self.repo, check=False)
                except Exception:
                    pass
        for rid in ids:
            if self.run_deleted and rid == str(self.run_id or ""):
                continue
            try:
                log(f"deleting workflow run {rid} "
                    f"(artifact + logs)...")
                gh("run", "delete", rid,
                   "--repo", self.repo, check=False)
                if rid == str(self.run_id or ""):
                    self.run_deleted = True
            except Exception as e:
                log(f"WARNING: run delete failed for {rid}: {e}")
        if self.branch and self.branch not in PROTECTED_BRANCHES \
                and not self.branch_deleted:
            try:
                log(f"deleting temporary branch {self.branch}...")
                r = run(["gh", "api", "-X", "DELETE",
                         f"repos/{self.repo}/git/refs/heads/{self.branch}"],
                        check=False)
                if r.returncode != 0:
                    # Fallback: push --delete via gh credential helper.
                    env = os.environ.copy()
                    run(["git", "-c",
                         "credential.helper=!gh auth git-credential",
                         "push", "origin", "--delete", self.branch],
                        cwd=self.clone_dir or ".", check=False, env=env)
                self.branch_deleted = True
            except Exception as e:
                log(f"WARNING: branch delete failed: {e}")
        for path, is_dir in ((self.archive, False),
                             (self.clone_dir, True)):
            if not path:
                continue
            try:
                p = Path(path)
                if is_dir and p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                    log(f"removed temp dir {p}")
                elif not is_dir and p.is_file():
                    p.unlink()
                    log(f"removed temp archive {p}")
            except Exception as e:
                log(f"WARNING: temp cleanup failed for {path}: {e}")


def find_run(repo, branch, since_epoch, timeout_s=120,
             workflow=WORKFLOW_FILE):
    """Poll `gh run list` for our run on a branch.

    The branch name is unique per build, so any run on it is ours
    (newest first if several exist).
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = gh("run", "list", "--workflow", workflow,
               "--branch", branch, "--limit", "10",
               "--json",
               "databaseId,event,createdAt,headBranch,status,conclusion",
               "--repo", repo, check=False)
        try:
            runs = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            runs = []
        if runs:
            def created_ts(x):
                try:
                    dt = datetime.datetime.fromisoformat(
                        x["createdAt"].replace("Z", "+00:00"))
                    return dt.timestamp()
                except Exception:
                    return 0
            fresh = [x for x in runs if created_ts(x) >= since_epoch - 30]
            pool = fresh or runs
            dispatch = [x for x in pool if x.get("event") == "workflow_dispatch"]
            chosen = sorted(dispatch or pool,
                            key=created_ts, reverse=True)[0]
            return str(chosen["databaseId"])
        time.sleep(5)
    raise RuntimeError(f"timed out waiting for a workflow run on branch "
                       f"{branch} (is Actions enabled? check the repo "
                       f"Actions tab).")


def seed_shared_cache(repo, run_id, slug, cache_key, apt_key, state,
                      no_seed=False, timeout_mins=15):
    """Save this build's dependencies to main-scope cache for the next
    build of the same project.

    The temp run uploaded a `cache-payload-<slug>` artifact (registries,
    target/). A short `seed-cache.yml` run ON the default branch
    downloads it and saves it via actions/cache -- the save lands in
    main scope, which every future temp branch can restore from. The
    seeder run is registered for cleanup (cache entries survive run
    deletion). Returns True when the next build can expect a warm
    cache. Never raises; warns instead.
    """
    if no_seed:
        log("cache seeding skipped (--no-seed).")
        return False
    try:
        names = gh("api", f"repos/{repo}/actions/runs/{run_id}/artifacts",
                   "--jq", ".artifacts[].name",
                   timeout=120).stdout.split()
    except RuntimeError as e:
        log(f"WARNING: cannot list artifacts, skipping seeding: {e}")
        return False
    if f"cache-payload-{slug}" not in names:
        log("no cache payload uploaded; skipping cache seeding.")
        return False
    try:
        main = gh("repo", "view", repo, "--json", "defaultBranchRef",
                  "--jq", ".defaultBranchRef.name").stdout.strip() or "main"
    except RuntimeError:
        main = "main"
    since = time.time()
    trig = gh("workflow", "run", "seed-cache.yml", "--ref", main,
              "--repo", repo,
              "-f", f"slug={slug}",
              "-f", f"payload_run_id={run_id}",
              "-f", f"cache_key={cache_key}",
              "-f", f"apt_key={apt_key}", check=False)
    if trig.returncode != 0:
        log("WARNING: seed-cache dispatch failed; next build stays cold.")
        return False
    # Identify OUR seeder by its run title (several may share main).
    deadline = time.time() + 180
    seed_id = None
    while time.time() < deadline:
        r = gh("run", "list", "--workflow", "seed-cache.yml",
               "--branch", main, "--limit", "10",
               "--json", "databaseId,displayTitle,createdAt",
               "--repo", repo, check=False)
        try:
            runs = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            runs = []
        mine = [x for x in runs
                if str(run_id) in (x.get("displayTitle") or "")
                and _ts(x.get("createdAt", "")) >= since - 30]
        if mine:
            seed_id = str(sorted(
                mine, key=lambda x: x.get("createdAt", ""))[-1]["databaseId"])
            break
        time.sleep(5)
    if seed_id is None:
        log("WARNING: seeder run not found; next build stays cold.")
        return False
    state.extra_runs.append(seed_id)
    say(f"seed run: https://github.com/{repo}/actions/runs/{seed_id}")
    pulse_start("seeding shared cache on main")
    try:
        watch = gh("run", "watch", seed_id, "--repo", repo,
                   "--exit-status", "--interval", "10",
                   check=False, timeout=timeout_mins * 60 + 60)
    finally:
        pulse_stop()
    try:
        concl = gh("run", "view", seed_id, "--repo", repo,
                   "--json", "conclusion",
                   "--jq", ".conclusion").stdout.strip()
    except RuntimeError:
        concl = ""
    if (watch.returncode == 0 and concl in ("", "success")) \
            or concl == "success":
        say(f"shared cache seeded: {cache_key}")
        return True
    log(f"WARNING: seeder {concl or 'failed'}; next build stays cold.")
    return False


def _ts(iso):
    try:
        return datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build current project files on GitHub Actions "
                    "without committing them locally.")
    ap.add_argument("--command", default=os.environ.get(
        "REMOTE_BUILD_COMMAND", DEFAULT_COMMAND),
        help=f"build command run as 'cd project && <cmd>' "
             f"(default: {DEFAULT_COMMAND!r})")
    ap.add_argument("--repo", default=os.environ.get("REMOTE_BUILD_REPO",
                                                     DEFAULT_REPO),
                    help=f"build repo OWNER/REPO (or $REMOTE_BUILD_REPO; "
                         f"default: {DEFAULT_REPO})")
    ap.add_argument("--apt", default=os.environ.get(
        "REMOTE_BUILD_APT_PACKAGES", ""),
        help="extra apt packages, space separated")
    ap.add_argument("--output", default="remote-build-output",
                    help="directory for the downloaded binary")
    ap.add_argument("--timeout-mins", type=int, default=45,
                    help="max minutes to wait for the run")
    ap.add_argument("--raw-log", action="store_true",
                    help="print the full unfiltered `gh run view --log` "
                         "output (default prints a cleaned compiler log)")
    ap.add_argument("--verbose", action="store_true",
                    help="print all progress lines (default is quiet: a "
                         "live one-line progress indicator plus errors "
                         "only)")
    ap.add_argument("--no-seed", action="store_true",
                    help="skip seeding the main-scope shared cache after "
                         "a successful build (default seeds it, so the "
                         "next build of the same project is warm)")
    args = ap.parse_args(argv)
    global _QUIET
    _QUIET = not args.verbose

    # 1-2. Tool + auth checks.
    ok = all(check_tool(t) for t in ("python", "git", "gh"))
    if not ok:
        return 1
    try:
        gh("auth", "status")
    except RuntimeError as e:
        err(f"GitHub authentication failed.\n{e}\nRun: gh auth login")
        return 1

    # 3. Build repo: flag, else env, else built-in default.
    if not args.repo:
        args.repo = DEFAULT_REPO
    try:
        parse_repo(args.repo)
    except RuntimeError as e:
        err(str(e))
        return 1
    repo = args.repo
    try:
        url = gh("repo", "view", repo, "--json", "url",
                 "--jq", ".url").stdout.strip()
        log(f"build repo OK: {url}")
    except RuntimeError as e:
        err(f"cannot access build repo {repo}.\n{e}")
        return 1

    project_root = Path.cwd()
    build_id, branch = generate_build_id()
    artifact = f"{ARTIFACT_PREFIX}-{build_id}"
    slug, lock16, cmd12, cache_key, cache_prefix = cache_key_for(
        project_root, args.command)
    apt_hash = (hashlib.sha256(args.apt.encode()).hexdigest()[:12]
                if args.apt.strip() else "")
    apt_key = f"remote-build-apt-{apt_hash}" if apt_hash else ""
    log(f"build ID: {build_id}")
    log(f"branch:   {branch}")
    log(f"command:  {args.command}")
    log(f"cache key: {cache_key}")

    if branch in PROTECTED_BRANCHES or build_id in PROTECTED_BRANCHES:
        err(f"refusing to use protected branch name {branch}")
        return 1

    state = Cleanup(repo)
    interrupted = {"flag": False}

    def on_signal(signum, frame):
        interrupted["flag"] = True
        pulse_stop()
        say("interrupted; cancelling and cleaning up...")
        state.cleanup(cancel_first=True)
        sys.exit(130)

    old_int = signal.signal(signal.SIGINT, on_signal)
    old_term = signal.signal(signal.SIGTERM, on_signal)

    try:
        # 4-5. Temporary archive (never a commit in the user repo).
        fd, tmppath = tempfile.mkstemp(prefix="remote-build-",
                                       suffix=".tar.gz")
        os.close(fd)
        state.archive = tmppath
        archive = Path(tmppath)
        create_archive(project_root, archive)

        # 6-8. Clone build repo to temp dir, temp branch, upload contents.
        clone_dir = Path(tempfile.mkdtemp(prefix="remote-build-clone-"))
        state.clone_dir = str(clone_dir)
        state.branch = branch
        gh_cred = ["-c", "credential.helper=!gh auth git-credential"]
        repo_url = f"https://github.com/{repo}.git"
        log(f"cloning build repo (shallow)...")
        run(["git", *gh_cred, "clone", "--depth", "1",
             repo_url, str(clone_dir)], check=True)
        run(["git", "checkout", "-b", branch], cwd=clone_dir)
        proj = clone_dir / "project"
        if proj.exists():
            shutil.rmtree(proj)
        proj.mkdir(parents=True)
        staging = Path(tempfile.mkdtemp(prefix="remote-build-stage-"))
        try:
            safe_extract(archive, staging)
            for item in staging.iterdir():
                dest = proj / item.name
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        # Fail fast before pushing: a missing manifest here means the
        # remote `cd project && <command>` could never succeed.
        check_staged_project(proj, args.command)
        log("staged project/ OK "
            f"({len(list(proj.rglob('*')))} entries).")
        (clone_dir / "remote-build-command.txt").write_text(
            args.command + "\n")
        (clone_dir / "remote-build-apt.txt").write_text(
            (args.apt + "\n") if args.apt else "")
        (clone_dir / "remote-build-id.txt").write_text(build_id + "\n")
        run(["git", "add", "-A"], cwd=clone_dir)
        status = run(["git", "status", "--porcelain"],
                     cwd=clone_dir).stdout.strip()
        if not status:
            raise RuntimeError("nothing to commit (archive was empty?)")
        # Always override the identity in the throwaway clone: the
        # machine's global git config may use a private email, which
        # GitHub rejects on push (GH007). The numeric noreply address
        # (ID+login form) always passes. This touches only the temp
        # clone's local config, never the user's repo or global config.
        try:
            login = gh("api", "user", "--jq", ".login").stdout.strip()
            uid = gh("api", "user", "--jq", ".id").stdout.strip()
        except RuntimeError:
            login, uid = "remote-build", ""
        noreply = (f"{uid}+{login}@users.noreply.github.com"
                   if uid and login else
                   f"{login or 'remote-build'}@users.noreply.github.com")
        run(["git", "config", "user.name", login or "remote-build"],
            cwd=clone_dir)
        run(["git", "config", "user.email", noreply], cwd=clone_dir)
        log(f"set temp-clone git identity to '{noreply}'.")
        run(["git", "commit", "-m", f"Temporary build {build_id}"],
            cwd=clone_dir)
        log(f"pushing temporary branch {branch}...")
        run(["git", *gh_cred, "push", "-u", "origin", branch],
            cwd=clone_dir)

        # 9-10. Trigger via documented workflow_dispatch on our ref.
        since = time.time()
        log("triggering build.yml via workflow_dispatch...")
        trig = gh("workflow", "run", WORKFLOW_FILE, "--ref", branch,
                  "--repo", repo,
                  "-f", f"build_id={build_id}",
                  "-f", f"build_command={args.command}",
                  "-f", f"apt_packages={args.apt}",
                  "-f", f"cache_slug={slug}",
                  "-f", f"cache_lock={lock16}",
                  "-f", f"cache_cmd={cmd12}",
                  "-f", f"apt_hash={apt_hash}", check=False)
        if trig.returncode != 0:
            raise RuntimeError(
                "workflow_dispatch failed (build.yml is dispatch-only; "
                "pushing the branch alone triggers nothing). "
                "Is build.yml on the build repo's default branch, and "
                "does your token allow running workflows?")

        # 11. Find exact run (unique branch => no cross-talk).
        log("locating workflow run...")
        run_id = find_run(repo, branch, since_epoch=since)
        state.run_id = run_id
        say(f"run: https://github.com/{repo}/actions/runs/{run_id}")

        # 12-13. Wait (showing the real run phase, not a blind spinner),
        # then print the compiler/build output.
        run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
        concl = poll_run_to_completion(repo, run_id, run_url,
                                       timeout_mins=args.timeout_mins)
        if not concl:
            err("timed out waiting for the remote build.")
            return 1
        viewed = gh("run", "view", run_id, "--repo", repo,
                    "--log", check=False, timeout=600)
        cleaned = clean_log(viewed.stdout)
        if args.raw_log:
            say("---- remote build log (raw `gh run view --log`) ----")
            print(viewed.stdout, end="")
            say("---- end of remote build log ----")
        elif not _QUIET:
            say("---- remote build log (cleaned; --raw-log for full) ----")
            print(cleaned, end="")
            say("---- end of remote build log ----")
        if viewed.stderr:
            print(viewed.stderr, end="", file=sys.stderr)

        success = concl == "success"
        if interrupted["flag"]:
            return 130
        if not success:
            err(f"remote build failed (conclusion={concl}). "
                f"No binary was downloaded: the workflow uploads the "
                f"artifact only on success, so there is nothing to pull. "
                f"Cleanup below still deletes the run and branch.")
            if _QUIET and not args.raw_log:
                say("---- failing step (re-run with --verbose or "
                    "--raw-log for everything) ----")
                print(error_section(cleaned), end="")
            return 1

        # 14-16. Success: download the BINARY artifact by its own URL.
        # The run may also hold a cache-payload artifact (dependency
        # tarballs for the seeder); it must never be mistaken for the
        # binary. We resolve the artifact id via the API (waiting out
        # listing lag), then fetch that exact zip -- the same bytes the
        # Actions UI links to -- and extract it ourselves.
        out_dir = (project_root / args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in out_dir.iterdir()}
        kind, aid, aname = wait_for_binary_artifact(
            repo, run_id, artifact)
        if kind in ("exact", "legacy"):
            log(f"downloading artifact {aname} (id {aid})...")
            pulse_start("downloading binary")
            try:
                ok = download_artifact_zip(repo, aid, aname, out_dir)
            finally:
                pulse_stop()
            if not ok:
                return 1
        elif kind == "payload-only":
            # Check-type command: nothing to download -- still a
            # success, and seeding can use the payload.
            say("remote build SUCCEEDED (no binary artifact: "
                "expected for check-type commands such as "
                "`cargo check`; nothing to download).")
            seed_shared_cache(repo, run_id, slug, cache_key, apt_key,
                              state, no_seed=args.no_seed)
            return 0
        elif kind == "empty":
            say("remote build SUCCEEDED (no artifacts uploaded; "
                "nothing to download).")
            return 0
        else:
            err("artifact download failed (nothing uploaded?).")
            return 1
        purge_cache_payload(out_dir)
        flatten_output_dir(out_dir, before)
        files = [p for p in out_dir.rglob("*") if p.is_file()]
        if not files:
            err("download produced no files.")
            return 1
        for f in files:
            say(f"binary: {f} ({f.stat().st_size} bytes)")
        seed_shared_cache(repo, run_id, slug, cache_key, apt_key, state,
                          no_seed=args.no_seed)
        say("remote build SUCCEEDED.")
        return 0
    except SystemExit as e:
        return int(e.code or 0)
    except RuntimeError as e:
        err(str(e))
        return 1
    except subprocess.TimeoutExpired:
        err("timed out waiting for the remote build.")
        return 1
    finally:
        # 17-20. Always: delete run (artifact+logs), branch, local temps.
        try:
            state.cleanup(cancel_first=False)
        finally:
            try:
                signal.signal(signal.SIGINT, old_int)
                signal.signal(signal.SIGTERM, old_term)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
