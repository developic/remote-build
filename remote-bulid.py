import fnmatch
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = "developic/remote-build"
DEFAULT_BRANCH = "release/zip-20261007"


def run(cmd, cwd=".", check=True, env=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result


def key_needs_passphrase(key: Path):
    """True if the private key is encrypted (empty passphrase does not work)."""
    result = subprocess.run(
        ["ssh-keygen", "-y", "-P", "", "-f", str(key)],
        capture_output=True,
    )
    return result.returncode != 0


def ensure_ssh_agent(root):
    """Return env with a live ssh-agent (starting one if needed)."""
    if shutil.which("ssh-agent") is None:
        raise RuntimeError("ssh-agent not found in PATH.")
    env = os.environ.copy()
    sock = env.get("SSH_AUTH_SOCK")
    if sock and Path(sock).exists():
        probe = subprocess.run(["ssh-add", "-l"], capture_output=True, env=env)
        if probe.returncode in (0, 1):  # 0 = has keys, 1 = alive, no keys
            return env
    out = run(["ssh-agent", "-s"], cwd=root).stdout
    for token in out.replace(";", " ").split():
        name, sep, value = token.partition("=")
        if sep and name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID"):
            env[name] = value
    if "SSH_AUTH_SOCK" not in env:
        raise RuntimeError("could not start ssh-agent.")
    print("[stage 3] ssh-agent started.")
    return env


def _secure_unlink(path: str):
    try:
        size = os.path.getsize(path)
        with open(path, "wb") as f:
            f.write(b"\x00" * size)
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def ssh_add_with_prompt(key: Path, env):
    """Ask for the SSH passphrase in code (getpass) and add the key.

    The passphrase is passed to ssh-add via an SSH_ASKPASS helper so it
    never appears in process arguments; temp files are wiped + removed.
    """
    try:
        passphrase = getpass.getpass(f"Enter SSH passphrase for {key}: ")
    except Exception as e:
        raise RuntimeError(
            f"cannot prompt for SSH passphrase ({e}); "
            "run in a terminal, or omit --ssh to push via gh (HTTPS)."
        )
    if not passphrase:
        raise RuntimeError("empty SSH passphrase.")
    pw_fd, pw_path = tempfile.mkstemp(prefix="ssh-pw-")
    ap_fd, ap_path = tempfile.mkstemp(prefix="ssh-askpass-", suffix=".sh")
    try:
        os.write(pw_fd, passphrase.encode())
        os.close(pw_fd)
        os.chmod(pw_path, 0o600)
        with os.fdopen(ap_fd, "w") as f:
            f.write(f"#!/bin/sh\ncat '{pw_path}'\n")
        os.chmod(ap_path, 0o700)
        add_env = dict(
            env, SSH_ASKPASS=ap_path, SSH_ASKPASS_REQUIRE="force", DISPLAY=":0"
        )
        # setsid detaches any controlling terminal so ssh-add cannot block
        # on /dev/tty and is forced to use the SSH_ASKPASS helper.
        add_cmd = ["ssh-add", str(key)]
        if shutil.which("setsid") is not None:
            add_cmd = ["setsid", *add_cmd]
        result = subprocess.run(
            add_cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=add_env,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ssh-add failed (wrong passphrase?): {result.stderr.strip()}"
            )
        print(f"[stage 3] SSH key loaded: {key}")
    finally:
        passphrase = ""  # noqa: F841 - drop reference (best effort)
        _secure_unlink(pw_path)
        _secure_unlink(ap_path)


def push_via_ssh(root: Path, branch: str, ssh_key=None):
    """Push the branch over the ssh remote, asking for passphrase in code."""
    key = Path(ssh_key).expanduser() if ssh_key else Path.home() / ".ssh" / "id_ed25519"
    if not key.is_file():
        raise RuntimeError(f"SSH key not found: {key} (pass --ssh-key PATH).")
    env = ensure_ssh_agent(root)

    fp = subprocess.run(
        ["ssh-keygen", "-lf", str(key)], capture_output=True, text=True
    )
    fingerprint = fp.stdout.split()[1] if fp.returncode == 0 else ""
    listed = subprocess.run(
        ["ssh-add", "-l"], capture_output=True, text=True, env=env
    )
    if not (fingerprint and fingerprint in listed.stdout):
        if key_needs_passphrase(key):
            ssh_add_with_prompt(key, env)
        else:
            run(["ssh-add", str(key)], cwd=root, env=env)
            print(f"[stage 3] SSH key loaded (no passphrase): {key}")
    else:
        print(f"[stage 3] SSH key already in agent: {key}")

    run(["git", "push", "-u", "origin", branch], cwd=root, env=env)
    print(f"[stage 3] pushed branch '{branch}' to origin (SSH).")
    return env


def is_git_repo(path="."):
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except FileNotFoundError:
        return False


def load_gitignore_patterns(root: Path):
    patterns = []
    gitignore = root / ".gitignore"
    if gitignore.is_file():
        for line in gitignore.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    # Always ignore .git/ and the output zip itself
    patterns.extend([".git/", "*.zip", "__pycache__/"])
    return patterns


def is_ignored(rel_posix: str, is_dir: bool, patterns):
    for pat in patterns:
        # directory-only pattern e.g. "build/"
        if pat.endswith("/"):
            if rel_posix == pat[:-1] or rel_posix.startswith(pat):
                return True
            continue
        if fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(
            os.path.basename(rel_posix), pat
        ):
            return True
        # match prefix for patterns without wildcard like "*.pyc" handled above,
        # plain dir name like "dist" should ignore "dist/..."
        if "/" not in pat and "*" not in pat and "?" not in pat:
            if rel_posix == pat or rel_posix.startswith(pat + "/"):
                return True
    return False


def list_files_git(root: Path):
    """List non-ignored files using git (respects .gitignore exactly)."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    files = [f for f in result.stdout.decode().split("\0") if f]
    return [root / f for f in files if (root / f).is_file()]


def list_files_manual(root: Path):
    patterns = load_gitignore_patterns(root)
    collected = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored dirs early + always skip .git
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        # modify dirnames in-place to skip ignored dirs
        keep = []
        for d in dirnames:
            rel = f"{rel_dir}/{d}".lstrip("/") if rel_dir else d
            if d == ".git" or is_ignored(rel, True, patterns):
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            rel = f"{rel_dir}/{fn}".lstrip("/") if rel_dir else fn
            if is_ignored(rel, False, patterns):
                continue
            collected.append(Path(dirpath) / fn)
    return collected


def stage_one(root: Path):
    print("[stage 1] checking git repository...")
    if is_git_repo(root):
        print("[stage 1] OK: project is a git repository.")
        return True
    print("[stage 1] FAIL: project is NOT a git repository.", file=sys.stderr)
    return False


def stage_two(root: Path, output: Path):
    print("[stage 2] building release zip (respecting .gitignore)...")
    if is_git_repo(root):
        files = list_files_git(root)
        if files is None:
            print("[stage 2] git listing failed, falling back to manual scan.")
            files = list_files_manual(root)
        else:
            print(f"[stage 2] git ls-files found {len(files)} file(s).")
    else:
        files = list_files_manual(root)
        print(f"[stage 2] manual scan found {len(files)} file(s).")

    if output.exists():
        output.unlink()
    # Don't include the output zip inside itself, and skip other
    # root-level *.zip artifacts to avoid zip-in-zip recursion.
    filtered = []
    for f in files:
        if f.resolve() == output.resolve():
            continue
        if f.suffix.lower() == ".zip":
            print(f"[stage 2] skipped zip artifact: {f.relative_to(root).as_posix()}")
            continue
        filtered.append(f)
    files = filtered

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(files):
            arcname = f.relative_to(root).as_posix()
            zf.write(f, arcname)
            print(f"[stage 2] added: {arcname}")

    size = output.stat().st_size
    print(f"[stage 2] OK: wrote {output} ({len(files)} files, {size} bytes).")
    return output


def stage_three(root: Path, output: Path, branch: str, repo: str = REPO,
                use_ssh=False, ssh_key=None):
    print(f"[stage 3] pushing zip-only branch '{branch}' to {repo} (via gh)...")
    if shutil.which("gh") is None:
        print("[stage 3] FAIL: 'gh' CLI not found in PATH.", file=sys.stderr)
        sys.exit(1)
    gh = lambda *args: run(["gh", *args], cwd=root)  # noqa: E731

    # gh auth + repo checks (uses gh, not git)
    try:
        gh("auth", "status")
    except RuntimeError as e:
        print(f"[stage 3] FAIL: gh not authenticated.\n{e}", file=sys.stderr)
        sys.exit(1)
    try:
        url = gh("repo", "view", repo, "--json", "url", "--jq", ".url").stdout.strip()
    except RuntimeError as e:
        print(f"[stage 3] FAIL: cannot access repo {repo}.\n{e}", file=sys.stderr)
        sys.exit(1)
    print(f"[stage 3] repo OK: {url}")

    if not output.is_file():
        print(f"[stage 3] FAIL: zip not found: {output}", file=sys.stderr)
        sys.exit(1)

    # ensure git identity exists for the commit (local repo config only)
    ident = run(["git", "config", "user.name"], cwd=root, check=False)
    if not ident.stdout.strip():
        try:
            login = gh("api", "user", "--jq", ".login").stdout.strip()
        except RuntimeError:
            login = "remote-bulid"
        run(["git", "config", "user.name", login], cwd=root)
        run(
            ["git", "config", "user.email", f"{login}@users.noreply.github.com"],
            cwd=root,
        )
        print(f"[stage 3] set local git identity to '{login}'.")

    rel_zip = output.relative_to(root).as_posix()
    # create (or reuse) the branch
    exists = run(
        ["git", "rev-parse", "--verify", "--quiet", branch], cwd=root, check=False
    )
    if exists.returncode == 0:
        run(["git", "switch", branch], cwd=root)
        print(f"[stage 3] switched to existing local branch '{branch}'.")
    else:
        run(["git", "switch", "-c", branch], cwd=root)
        print(f"[stage 3] created branch '{branch}'.")

    # stage ONLY the zip file
    run(["git", "add", "-f", "--", rel_zip], cwd=root)
    staged = run(["git", "diff", "--cached", "--name-only"], cwd=root).stdout.strip()
    if not staged:
        print("[stage 3] nothing new to commit (zip unchanged).")
    else:
        sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=root).stdout.strip()
        run(
            ["git", "commit", "-m", f"Release zip build (from {sha})", "--", rel_zip],
            cwd=root,
        )
        print(f"[stage 3] committed: {staged}")

    if use_ssh:
        # SSH mode: ask passphrase in code, push over the ssh remote.
        try:
            ssh_env = push_via_ssh(root, branch, ssh_key)
        except RuntimeError as e:
            print(f"[stage 3] FAIL: {e}", file=sys.stderr)
            sys.exit(1)
        ls = run(
            ["git", "ls-remote", "--heads", "origin", branch], cwd=root, env=ssh_env
        ).stdout.strip()
    else:
        # Push over HTTPS using gh as credential helper (works without
        # ssh-agent; origin remote itself is left untouched).
        gh_git = [
            "-c",
            "credential.helper=!gh auth git-credential",
            "-c",
            "url.https://github.com/.insteadOf=git@github.com:",
        ]
        run(["git", *gh_git, "push", "-u", "origin", branch], cwd=root)
        print(f"[stage 3] pushed branch '{branch}' to origin.")
        ls = run(
            ["git", *gh_git, "ls-remote", "--heads", "origin", branch], cwd=root
        ).stdout.strip()

    # confirm via origin + show gh URL
    if branch not in ls:
        print(f"[stage 3] FAIL: branch '{branch}' not found on origin.", file=sys.stderr)
        sys.exit(1)
    print(f"[stage 3] OK: {url}/tree/{branch}")
    return branch


def main():
    root = Path.cwd()
    output = root / "release.zip"
    branch = DEFAULT_BRANCH
    push = False
    repo = REPO
    use_ssh = False
    ssh_key = None

    args = sys.argv[1:]
    i = 0
    positionals = []
    while i < len(args):
        a = args[i]
        if a == "--push":
            push = True
        elif a == "--ssh":
            use_ssh = True
            push = True
        elif a == "--ssh-key" and i + 1 < len(args):
            ssh_key = args[i + 1]
            use_ssh = True
            push = True
            i += 1
        elif a == "--branch" and i + 1 < len(args):
            branch = args[i + 1]
            i += 1
        elif a == "--repo" and i + 1 < len(args):
            repo = args[i + 1]
            i += 1
        elif a in ("-h", "--help"):
            print(
                "usage: remote-bulid.py [output.zip] [--push] "
                "[--branch NAME] [--repo OWNER/REPO] "
                "[--ssh] [--ssh-key PATH]\n"
                "  --push      run stage 3: push zip-only branch (via gh HTTPS)\n"
                "  --ssh       run stage 3 over SSH; asks SSH passphrase in code\n"
                "  --ssh-key   SSH private key path (implies --ssh)"
            )
            return
        else:
            positionals.append(a)
        i += 1
    if positionals:
        output = (root / positionals[0]).resolve()

    if not stage_one(root):
        sys.exit(1)
    output = stage_two(root, output)
    if push:
        stage_three(root, output, branch, repo, use_ssh, ssh_key)
    else:
        print("[stage 3] skipped (pass --push to upload zip-only branch via gh).")


if __name__ == "__main__":
    main()
