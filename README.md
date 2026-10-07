# remote-rust-build

Temporary remote Rust build machine powered by GitHub Actions.

Run `./remote-build.sh` inside your Rust project and it will:

```text
pack local project
→ send it to this repository on a temporary branch
→ start GitHub Actions
→ wait for the build
→ show build errors
→ return the correct exit code
→ clean up all temporary data
```

You never have to commit your Rust changes just to test-build them remotely.

## How it works

1. `remote-build.sh` (lives in your Rust project, copy it from this repo)
   creates a `tar.gz` of the current filesystem state — uncommitted
   changes included, `.git/` and `target/` excluded.
2. It shallow-clones this repository, creates a unique branch
   `remote-build/<timestamp>-<random>`, extracts your source into
   `project/`, writes the requested cargo command to
   `remote-build-command.txt`, commits, and pushes.
3. Pushing a `remote-build/**` branch automatically triggers
   `.github/workflows/build.yml` (`on: push`, no manual dispatch, no
   invented ZIP-upload API).
4. The script polls the run (matched by commit SHA, so concurrent builds
   never mix), prints the real compiler log via `gh run view`, and sets
   the exit code from the workflow conclusion.
5. On success it optionally downloads the release binary artifact, then
   deletes the temporary branch and all local temp files — on success,
   failure, timeout, or `Ctrl+C` (via `trap`).

This repository stays clean: only `remote-build.sh`,
`.github/workflows/build.yml`, and this README live here permanently.
Every build's source, branch, and artifact are deleted afterwards.

## Setup

### 1. Create the dedicated GitHub repository

Create an empty repo named `remote-rust-build` (private recommended,
so your source is only visible to you) and push this project into it:

```bash
gh repo create OWNER/remote-rust-build --private --source=. --push
```

Replace `OWNER` with your GitHub user or organization.

### 2. Authenticate with `gh`

```bash
gh auth login
```

Verify with:

```bash
gh auth status
```

Never put tokens in the script. Auth comes from the GitHub CLI.

### 3. Configure `REMOTE_BUILD_REPO`

```bash
export REMOTE_BUILD_REPO="OWNER/remote-rust-build"
```

Optional variables (with defaults):

```bash
REMOTE_BUILD_WORKFLOW="build.yml"          # workflow file to watch
REMOTE_BUILD_TIMEOUT=1800                  # seconds before giving up
REMOTE_BUILD_COMMAND="cargo build --release"
REMOTE_BUILD_DOWNLOAD_BINARY=1             # 1 = fetch binary, 0 = skip
REMOTE_BUILD_OUTPUT_DIR="remote-build-output"
```

No `config.json`, no `.env` file needed.

### 4. Make `remote-build.sh` executable

Copy `remote-build.sh` from this repo into your Rust project root, then:

```bash
chmod +x remote-build.sh
```

### 5. Run the first build

```bash
cd my-rust-project
./remote-build.sh
```

## Cleanup

After every build the script deletes:

- local temporary archive and directories (`mktemp -d`)
- the temporary remote branch (`git push origin --delete`, idempotent)
- the temporary uploaded source (it only ever existed on that branch)
- the workflow artifact (after download, best effort, retention-days: 1)

Cleanup runs via `trap ... EXIT INT TERM`, so it also happens when
cargo fails, the workflow fails, the network fails, the timeout hits,
or you press `Ctrl+C`. The runner itself is ephemeral; the workflow
additionally tidies its workspace in an `always()` step.

## Failure behavior

If compilation fails:

- no binary is downloaded,
- the compiler errors from the remote log are printed,
- the script exits non-zero (`echo $?` → non-zero),
- everything is still cleaned up.

Timeouts cancel the workflow run (`gh run cancel`) before cleanup.

## Ctrl+C

Pressing `Ctrl+C` stops the wait loop, cancels the GitHub Actions run
if still in flight, deletes the temporary branch, removes local temp
files, and exits cleanly.

## Downloading the compiled binary

With `cargo build --release` (the default) and
`REMOTE_BUILD_DOWNLOAD_BINARY=1` (the default), the workflow uploads
the release binary as the `remote-build-binary` artifact (retention:
1 day) and the script downloads it to:

```text
remote-build-output/remote-build-<timestamp>-<random>/
```

A unique subdirectory per build avoids overwriting unrelated files and
keeps concurrent builds safe. With `cargo check` there is no binary,
which is expected and not an error.

Disable downloads with:

```bash
REMOTE_BUILD_DOWNLOAD_BINARY=0 ./remote-build.sh
```

## Changing the build command

```bash
REMOTE_BUILD_COMMAND="cargo check" ./remote-build.sh
REMOTE_BUILD_COMMAND="cargo build --release" ./remote-build.sh
REMOTE_BUILD_COMMAND="cargo test --release" ./remote-build.sh
```

The command runs from the uploaded project's root (`project/` in the
runner). Any failure (non-zero cargo exit, workflow failure) makes the
script exit non-zero, so it composes with other local automation.

## Toolchain and caching

- If your project has `rust-toolchain` / `rust-toolchain.toml`, the
  workflow respects it (`dtolnay/rust-toolchain`); otherwise stable Rust
  is used. Nightly is never forced.
- `Swatinem/rust-cache` caches the Cargo registry and `project/target`,
  keyed by dependency/toolchain hashes — the uploaded source itself is
  never cached, so every build compiles your current code.

## Security

- Minimal workflow permissions (`contents: read`). Branch deletion uses
  your local `gh` credentials, not workflow secrets.
- No repository secrets are exposed to the build.
- The archive excludes `.git/`, `target/`, `.github/`, `.env*`,
  `*.pem`, `*.key`. Tokens are never printed or placed in arguments.
- Treat the runner as untrusted build code: it only gets the uploaded
  source, nothing else.

## Requirements

`bash`, `git`, `gh`, `tar`, `mktemp` on a normal Linux environment.
Nothing is installed silently.
