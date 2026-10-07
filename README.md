# remote-rust-build

Temporary remote build machine powered by GitHub Actions. Rust is just
the example — the same flow builds **Go, Node.js, or any command**.

From any project, run:

```bash
python remote_build.py
```

GitHub Actions builds your **exact current local files** — no commit in
your project required. The temporary commit lives **only** in this
dedicated build repository, on a unique throwaway branch that is
deleted afterwards.

```text
local project
  ↓ python makes temp .tar.gz (excludes .git/ target/ .env *.key *.pem)
  ↓ temp branch remote-build/<id> in THIS repo (dispatch-only trigger)
  ↓ upload source into project/ + trigger build.yml (workflow_dispatch)
  ↓ wait, print real compiler output
  ↓ download binary on success
  ↓ delete run (artifact+logs), delete branch, delete local temps
```

Only three files exist:

```text
remote_build.py                # copy into any project (stdlib + gh CLI only)
.github/workflows/build.yml   # cloud builder (this repo)
README.md                      # this file
```

No `remote-build.sh`, no shell scripts, no hardcoded tokens, no
releases / pull requests / issues / permanent tags.

## 1. Setup (once)

Create the dedicated build repo (private recommended) and push these
files to `main`:

```bash
gh repo create OWNER/remote-rust-build --private --source=. --push
```

Contents of `main` stay minimal: `remote_build.py`,
`.github/workflows/build.yml`, `README.md`.

## 2. Use (any project, any machine)

Prerequisites: `python`, `git`, `gh`, and `gh auth login`.

```bash
export REMOTE_BUILD_REPO=OWNER/remote-rust-build
cp /path/to/remote_build.py ./my-app/
cd my-app
python remote_build.py
```

Examples:

```bash
python remote_build.py --command "cargo build --release"   # default
python remote_build.py --command "cargo check"
python remote_build.py --command "go build -o dist/app ./..."
python remote_build.py --command "npm ci && npm run build"
python remote_build.py --command "make release" --apt "libssl-dev"
```

Flags:

```text
--command TEXT     shell run as: cd project && <command>
--repo OWNER/REPO  override $REMOTE_BUILD_REPO
--apt "pkgs..."    extra apt packages (also $REMOTE_BUILD_APT_PACKAGES)
--output DIR       binary download dir (default: remote-build-output/)
--timeout-mins N   wait limit (default: 45)
--raw-log          full `gh run view --log` (default: cleaned compiler log)
--verbose          print all progress lines (default is quiet: a live
                   one-line progress indicator plus errors only)
```

Exit code mirrors the remote build: `0` on success, non-zero on
failure. Only the cleaned compiler log is printed by default
(grouped under `### <step>` headers, runner plumbing removed);
pass `--raw-log` for the full output.
Before pushing, the client verifies `project/` actually contains the
manifest your command needs (e.g. `Cargo.toml` for `cargo ...`) and
aborts with a clear error if you ran from the wrong directory.
On success the binary lands in `remote-build-output/`. On failure no
binary is downloaded — the workflow uploads the artifact only on
success, so there is nothing to pull; fix the logged error and re-run.

## 3. How it works

1. **Checks.** Verifies `python`/`git`/`gh`, runs `gh auth status`,
   reads `$REMOTE_BUILD_REPO` (`OWNER/REPO`).
2. **Archive.** Builds a temp `.tar.gz` of the working tree. Respects
   `.gitignore` (via `git ls-files` when available) and always excludes
   `.git/ target/ node_modules/ dist/ .env *.key *.pem` and similar
   credential files. Your repo is never modified.
3. **Temp branch.** Shallow-clones this repo to a temp dir, creates a
   unique branch `remote-build/<utc>-<rand>`, extracts the archive
   into
   `project/`, writes `remote-build-command.txt` /
   `remote-build-apt.txt` / `remote-build-id.txt`, commits, pushes.
4. **Trigger.** Runs the documented
   `gh workflow run build.yml --ref <branch> -f build_id=... -f build_command=...`
   (dispatch-only: exactly one run per build).
5. **Find + wait.** Polls `gh run list --branch <branch>` — the branch
   is unique, so concurrent builds never mix — then
   `gh run watch --exit-status`.
6. **Logs.** Prints `gh run view --log` (normal Cargo / go / npm output).
7. **Download.** On success `gh run download -n remote-build-binary-<id>`.
8. **Cleanup (always).** `gh run delete <id>` (removes artifact + logs),
   deletes the temp branch via the API, removes the local archive and
   clone dir. `Ctrl+C` cancels the run first (`gh run cancel`), then
   cleans up. `main`/`master` are never touched.

Workflow details (`build.yml`): `workflow_dispatch` with per-project
cache key `remote-build-cache-<slug>-<lock>-<cmd>`, `permissions:
contents: read, actions: read`, checks out the temp branch, restores
the shared cache (read-only — temp branches can only read main-scope
entries), auto-detects Rust (`Cargo.toml`, respects
`rust-toolchain.toml`) / Go (`go.mod`) / Node (`package.json`), runs
your command, uploads `remote-build-binary-<build_id>` plus a
`cache-payload-<slug>` (both `retention-days: 1`). After success the
client dispatches `seed-cache.yml` on `main`, which saves the payload
to main-scope cache so the next build of the same project is warm
(`--no-seed` skips this).

## 4. Troubleshooting

| Symptom | Why | Fix |
|---|---|---|
| `REMOTE_BUILD_REPO is not set` | env var missing | `export REMOTE_BUILD_REPO=OWNER/remote-rust-build` |
| `gh: Not authenticated` | token expired | `gh auth login && gh auth status` |
| `workflow not found / ref not found` | `build.yml` missing on `main`, or push failed | ensure workflow is on `main`; check push log |
| `timed out waiting for run` | Actions disabled / queued | check repo Actions tab |
| `error[E...]` / linker failure | your code, or missing sys lib | fix code, or `--apt "libssl-dev"` |
| `artifact not found` | build failed so nothing uploaded | read the compiler log first; retry after fixing |
| branch delete `422` warning | already deleted concurrently | safe to ignore |

See `remote-build-explained.html` (if present) for a clickable
stage-by-stage diagram with success and failed log examples.
