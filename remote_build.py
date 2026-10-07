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
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

DEFAULT_COMMAND = "cargo build --release"
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


def log(msg):
    print(f"[remote-build] {msg}", flush=True)


def err(msg):
    print(f"[remote-build] ERROR: {msg}", file=sys.stderr, flush=True)


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


def parse_repo(repo):
    if "/" not in repo or repo.count("/") != 1:
        raise RuntimeError(
            f"invalid REMOTE_BUILD_REPO={repo!r}, expected OWNER/REPO")
    owner, name = repo.split("/")
    if not owner or not name or " " in repo:
        raise RuntimeError(f"invalid REMOTE_BUILD_REPO={repo!r}")
    return owner, name


def generate_build_id():
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    rnd = f"{random.getrandbits(32):08x}"
    return f"remote-build-{ts}-{rnd}"


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

    def cleanup(self, cancel_first=False):
        if cancel_first and self.run_id and not self.run_deleted:
            try:
                log(f"cancelling run {self.run_id}...")
                gh("run", "cancel", str(self.run_id),
                   "--repo", self.repo, check=False)
            except Exception:
                pass
        if self.run_id and not self.run_deleted:
            try:
                log(f"deleting workflow run {self.run_id} "
                    f"(artifact + logs)...")
                gh("run", "delete", str(self.run_id),
                   "--repo", self.repo, check=False)
                self.run_deleted = True
            except Exception as e:
                log(f"WARNING: run delete failed: {e}")
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


def find_run(repo, branch, since_epoch, timeout_s=120):
    """Poll `gh run list --branch <branch>` for our run.

    The branch name is unique per build, so any run on it is ours.
    Prefers the workflow_dispatch run when both push + dispatch exist.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = gh("run", "list", "--workflow", WORKFLOW_FILE,
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


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build current project files on GitHub Actions "
                    "without committing them locally.")
    ap.add_argument("--command", default=os.environ.get(
        "REMOTE_BUILD_COMMAND", DEFAULT_COMMAND),
        help=f"build command run as 'cd project && <cmd>' "
             f"(default: {DEFAULT_COMMAND!r})")
    ap.add_argument("--repo", default=os.environ.get("REMOTE_BUILD_REPO", ""),
                    help="build repo OWNER/REPO (or $REMOTE_BUILD_REPO)")
    ap.add_argument("--apt", default=os.environ.get(
        "REMOTE_BUILD_APT_PACKAGES", ""),
        help="extra apt packages, space separated")
    ap.add_argument("--output", default="remote-build-output",
                    help="directory for the downloaded binary")
    ap.add_argument("--timeout-mins", type=int, default=45,
                    help="max minutes to wait for the run")
    args = ap.parse_args(argv)

    # 1-2. Tool + auth checks.
    ok = all(check_tool(t) for t in ("python", "git", "gh"))
    if not ok:
        return 1
    try:
        gh("auth", "status")
    except RuntimeError as e:
        err(f"GitHub authentication failed.\n{e}\nRun: gh auth login")
        return 1

    # 3. Build repo from env (or flag).
    if not args.repo:
        err("REMOTE_BUILD_REPO is not set. "
            "Example: export REMOTE_BUILD_REPO=OWNER/remote-rust-build")
        return 1
    try:
        owner, _ = parse_repo(args.repo)
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
    build_id = generate_build_id()
    branch = build_id  # unique branch == unique build id
    artifact = f"{ARTIFACT_PREFIX}-{build_id}"
    log(f"build ID: {build_id}")
    log(f"command:  {args.command}")

    if branch in PROTECTED_BRANCHES:
        err(f"refusing to use protected branch name {branch}")
        return 1

    state = Cleanup(repo)
    interrupted = {"flag": False}

    def on_signal(signum, frame):
        interrupted["flag"] = True
        log("interrupted; cancelling and cleaning up...")
        state.cleanup(cancel_first=True)
        sys.exit(130)

    old_int = signal.signal(signal.SIGINT, on_signal)
    old_term = signal.signal(signal.SIGTERM, on_signal)

    exit_code = 1
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
                  "-f", f"apt_packages={args.apt}", check=False)
        if trig.returncode != 0:
            log("workflow_dispatch failed; falling back to push trigger "
                "(build.yml also runs on push to remote-build/**).")

        # 11. Find exact run (unique branch => no cross-talk).
        log("locating workflow run...")
        run_id = find_run(repo, branch, since_epoch=since)
        state.run_id = run_id
        log(f"run: https://github.com/{repo}/actions/runs/{run_id}")

        # 12-13. Wait, then print real compiler/build output.
        log(f"waiting up to {args.timeout_mins} min "
            f"(gh run watch)... Ctrl+C cancels.")
        watch = gh("run", "watch", run_id, "--repo", repo,
                   "--exit-status", "--interval", "10",
                   check=False, timeout=args.timeout_mins * 60 + 60)
        log("---- remote build log (gh run view --log) ----")
        viewed = gh("run", "view", run_id, "--repo", repo,
                    "--log", check=False)
        print(viewed.stdout, end="")
        if viewed.stderr:
            print(viewed.stderr, end="", file=sys.stderr)
        log("---- end of remote build log ----")

        concl = ""
        try:
            concl = gh("run", "view", run_id, "--repo", repo,
                       "--json", "conclusion",
                       "--jq", ".conclusion").stdout.strip()
        except RuntimeError:
            pass
        success = (watch.returncode == 0 and concl in ("", "success")) \
            or concl == "success"
        if interrupted["flag"]:
            return 130
        if not success:
            err(f"remote build failed "
                f"(conclusion={concl or 'failure'}).")
            return 1

        # 14-16. Success: download compiled binary.
        out_dir = (project_root / args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        log(f"downloading artifact {artifact}...")
        dl = gh("run", "download", run_id, "--repo", repo,
                "-n", artifact, "-D", str(out_dir), check=False)
        if dl.returncode != 0:
            log("exact artifact name not found; downloading all "
                "artifacts for the run...")
            dl = gh("run", "download", run_id, "--repo", repo,
                    "-D", str(out_dir), check=False)
            if dl.returncode != 0:
                err("artifact download failed (nothing uploaded?).")
                return 1
        files = [p for p in out_dir.rglob("*") if p.is_file()]
        if not files:
            err("download produced no files.")
            return 1
        for f in files:
            log(f"binary: {f} ({f.stat().st_size} bytes)")
        log("remote build SUCCEEDED.")
        exit_code = 0
        return 0
    except SystemExit as e:
        exit_code = int(e.code or 0)
        return exit_code
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
        if exit_code is None:
            exit_code = 1


if __name__ == "__main__":
    sys.exit(main())
