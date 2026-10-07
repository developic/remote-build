#!/usr/bin/env bash
#
# remote-build.sh — build the current Rust project on GitHub Actions
# without committing local changes.
#
# Usage:
#   export REMOTE_BUILD_REPO="OWNER/remote-rust-build"
#   ./remote-build.sh
#
# Configuration (environment variables, all optional except the first):
#   REMOTE_BUILD_REPO             OWNER/repo of the dedicated build repository (required)
#   REMOTE_BUILD_WORKFLOW         workflow file name, default: build.yml
#   REMOTE_BUILD_TIMEOUT          seconds to wait, default: 1800
#   REMOTE_BUILD_COMMAND          remote cargo command, default: cargo build --release
#   REMOTE_BUILD_DOWNLOAD_BINARY  1 to download the binary artifact, 0 to skip (default: 1)
#   REMOTE_BUILD_OUTPUT_DIR       download directory, default: remote-build-output
#
# Requires: bash, git, gh, tar, mktemp. Auth via `gh auth login`.

set -Eeuo pipefail

# ---------------------------------------------------------------- configuration

REMOTE_BUILD_REPO="${REMOTE_BUILD_REPO:-}"
REMOTE_BUILD_WORKFLOW="${REMOTE_BUILD_WORKFLOW:-build.yml}"
REMOTE_BUILD_TIMEOUT="${REMOTE_BUILD_TIMEOUT:-1800}"
REMOTE_BUILD_COMMAND="${REMOTE_BUILD_COMMAND:-cargo build --release}"
REMOTE_BUILD_DOWNLOAD_BINARY="${REMOTE_BUILD_DOWNLOAD_BINARY:-1}"
REMOTE_BUILD_OUTPUT_DIR="${REMOTE_BUILD_OUTPUT_DIR:-remote-build-output}"

# ------------------------------------------------------------------ utilities

die() {
    echo "Error: $*" >&2
    exit 1
}

step() {
    echo ""
    echo "[$1] $2"
}

info() {
    echo "  $*"
}

# ------------------------------------------------------------------ state
# Everything temporary is tracked here so cleanup() can remove it.
# Cleanup is idempotent: running it twice is safe.

TMPDIR_PATH=""
CLONE_DIR=""
BRANCH=""
RUN_ID=""
RUN_URL=""
HEAD_SHA=""
BUILD_FAILED=0
CLEANUP_DONE=0

cleanup() {
    # Preserve the exit code of the failed command, if any.
    local rc=$?
    if [ "$CLEANUP_DONE" -eq 1 ]; then
        return "$rc"
    fi
    CLEANUP_DONE=1

    # Never let a cleanup failure hide the real build error.
    set +e

    if [ "$BUILD_FAILED" -eq 0 ] && [ "$rc" -ne 0 ]; then
        BUILD_FAILED=1
    fi

    # If the run is still in flight (timeout / Ctrl+C), try to cancel it.
    if [ -n "$RUN_ID" ] && [ -n "$REMOTE_BUILD_REPO" ]; then
        local state
        state="$(gh run view "$RUN_ID" --repo "$REMOTE_BUILD_REPO" \
            --json status --jq '.status' 2>/dev/null || true)"
        if [ "$state" = "queued" ] || [ "$state" = "in_progress" ] \
            || [ "$state" = "requested" ] || [ "$state" = "waiting" ]; then
            echo ""
            info "Cancelling workflow run $RUN_ID..."
            gh run cancel "$RUN_ID" --repo "$REMOTE_BUILD_REPO" >/dev/null 2>&1 || true
        fi
    fi

    # Delete the temporary remote branch (idempotent).
    if [ -n "$BRANCH" ] && [ -n "$REMOTE_BUILD_REPO" ]; then
        if [ -n "$CLONE_DIR" ] && [ -d "$CLONE_DIR" ]; then
            git -C "$CLONE_DIR" push --quiet origin --delete "$BRANCH" >/dev/null 2>&1 || true
        else
            # Fallback when the local clone is gone: delete the ref via API.
            gh api --silent -X DELETE \
                "repos/${REMOTE_BUILD_REPO}/git/refs/heads/${BRANCH//\//%2F}" >/dev/null 2>&1 || true
        fi
    fi

    # Remove local temporary files.
    if [ -n "$TMPDIR_PATH" ] && [ -d "$TMPDIR_PATH" ]; then
        rm -rf "$TMPDIR_PATH" || true
    fi

    if [ "$BUILD_FAILED" -eq 0 ] && [ "$rc" -eq 0 ]; then
        info "Temporary build branch deleted."
        info "Temporary files deleted."
    fi

    return "$rc"
}

trap cleanup EXIT INT TERM

# ------------------------------------------------------------------ checks

for dep in git gh tar mktemp; do
    command -v "$dep" >/dev/null 2>&1 || {
        echo "Missing dependency: $dep" >&2
        if [ "$dep" = "gh" ]; then
            echo "Install GitHub CLI and authenticate with:" >&2
            echo "" >&2
            echo "    gh auth login" >&2
        fi
        exit 1
    }
done

gh auth status >/dev/null 2>&1 || {
    die "Not authenticated with GitHub. Run: gh auth login"
}

if [ -z "$REMOTE_BUILD_REPO" ]; then
    die "REMOTE_BUILD_REPO is not set. Example: export REMOTE_BUILD_REPO=\"OWNER/remote-rust-build\""
fi

if [ ! -f "Cargo.toml" ]; then
    die "No Cargo.toml in current directory. Run this script from your Rust project root."
fi

# ------------------------------------------------------------------ header

PROJECT_NAME="$(basename "$PWD")"

echo "Remote Rust Build"
echo "────────────────────────────────"
echo ""
echo "Project:    $PROJECT_NAME"
echo "Repository: $REMOTE_BUILD_REPO"
echo "Command:    $REMOTE_BUILD_COMMAND"
echo ""

# Unique ID per build so concurrent runs never share a branch.
# NOTE: $RANDOM (bash builtin) avoids the classic `tr ... | head -c`
# SIGPIPE pitfall, which exits non-zero under `set -o pipefail`.
UNIQUE_SUFFIX="$(date +%Y%m%d-%H%M%S)-$(printf '%04x%04x%04x' "$RANDOM" "$RANDOM" "$RANDOM")"
BRANCH="remote-build/${UNIQUE_SUFFIX}"

# ------------------------------------------------------------ [1/6] packing

step "1/6" "Packing project..."

TMPDIR_PATH="$(mktemp -d)"
CLONE_DIR="${TMPDIR_PATH}/build-repo"
ARCHIVE="${TMPDIR_PATH}/project.tar.gz"

# NOTE: .cargo/ and rust-toolchain* are intentionally INCLUDED (needed
# for the build). Excluded: VCS data, build output, CI config of the
# local project, and common secret files (never upload credentials).
tar -czf "$ARCHIVE" \
    --exclude=.git \
    --exclude=target \
    --exclude=.github \
    --exclude="$REMOTE_BUILD_OUTPUT_DIR" \
    --exclude=.env \
    --exclude='.env.*' \
    --exclude='*.pem' \
    --exclude='*.key' \
    -C "$PWD" .

# Sanity check: the archive must contain the build inputs.
# NOTE: plain `grep` (not `grep -q`) so it consumes all input; with
# `grep -q` + `set -o pipefail` tar can die of SIGPIPE and fail spuriously.
tar -tzf "$ARCHIVE" | grep "Cargo.toml" >/dev/null \
    || die "Archive does not contain Cargo.toml; nothing to build."
info "Packed $(du -h "$ARCHIVE" | cut -f1) (Cargo.toml + src + build files)."

# ---------------------------------------------------------- [2/6] uploading

step "2/6" "Uploading temporary source..."

# Shallow clone of the build repo; auth comes from `gh auth login`.
gh repo clone "$REMOTE_BUILD_REPO" "$CLONE_DIR" -- --quiet --depth 1 >/dev/null 2>&1 \
    || die "Cannot clone $REMOTE_BUILD_REPO. Check the name and your access (gh auth status)."

git -C "$CLONE_DIR" checkout --quiet -b "$BRANCH" \
    || die "Cannot create temporary branch $BRANCH."

mkdir -p "${CLONE_DIR}/project"
tar -xzf "$ARCHIVE" -C "${CLONE_DIR}/project"

# Tell the workflow which cargo command to run.
printf '%s\n' "$REMOTE_BUILD_COMMAND" > "${CLONE_DIR}/remote-build-command.txt"

git -C "$CLONE_DIR" add -A
git -C "$CLONE_DIR" -c user.name="remote-build" -c user.email="remote-build@noreply" \
    commit --quiet -m "remote-build ${UNIQUE_SUFFIX}: ${REMOTE_BUILD_COMMAND}" \
    || die "Cannot commit temporary build source."
git -C "$CLONE_DIR" push --quiet -u origin "$BRANCH" \
    || die "Cannot push temporary branch. Check your push access to $REMOTE_BUILD_REPO."

HEAD_SHA="$(git -C "$CLONE_DIR" rev-parse HEAD)"
info "Pushed temporary branch $BRANCH (${HEAD_SHA:0:12})."

# ------------------------------------------------------------ [3/6] starting

step "3/6" "Starting GitHub Actions..."

# The push trigger needs a few seconds to create the run. Poll for the
# run whose head SHA matches our temporary commit (concurrency-safe).
RUN_ID=""
for _ in $(seq 1 24); do
    RUN_ID="$(gh run list --repo "$REMOTE_BUILD_REPO" --branch "$BRANCH" \
        --limit 5 --json databaseId,headSha \
        --jq "[.[] | select(.headSha == \"$HEAD_SHA\")][0].databaseId // empty" 2>/dev/null || true)"
    if [ -n "$RUN_ID" ]; then
        break
    fi
    sleep 5
done

[ -n "$RUN_ID" ] || die "No workflow run appeared for $BRANCH. Check the Actions tab of $REMOTE_BUILD_REPO."

RUN_URL="$(gh run view "$RUN_ID" --repo "$REMOTE_BUILD_REPO" --json url --jq '.url' 2>/dev/null || true)"
info "Run: ${RUN_URL:-$RUN_ID}"

# ------------------------------------------------------------- [4/6] waiting

step "4/6" "Waiting for build (timeout ${REMOTE_BUILD_TIMEOUT}s)..."

START_TS="$(date +%s)"
STATUS=""
CONCLUSION=""
while true; do
    NOW_TS="$(date +%s)"
    if [ "$((NOW_TS - START_TS))" -ge "$REMOTE_BUILD_TIMEOUT" ]; then
        BUILD_FAILED=1
        die "Timeout after ${REMOTE_BUILD_TIMEOUT}s. Cancelling run... (${RUN_URL:-$RUN_ID})"
    fi
    SUMMARY="$(gh run view "$RUN_ID" --repo "$REMOTE_BUILD_REPO" \
        --json status,conclusion --jq '\(.status) \(.conclusion)' 2>/dev/null || true)"
    STATUS="${SUMMARY%% *}"
    CONCLUSION="${SUMMARY##* }"
    if [ "$STATUS" = "completed" ]; then
        break
    fi
    if [ -z "$STATUS" ]; then
        info "(status unavailable, retrying...)"
    else
        info "(status: $STATUS, elapsed: $((NOW_TS - START_TS))s)"
    fi
    sleep 10
done

echo ""
if [ "$CONCLUSION" = "success" ]; then
    info "Remote workflow concluded: success."
else
    info "Remote workflow concluded: ${CONCLUSION:-unknown}."
fi

# Show the real compiler output; fall back to the run URL.
echo ""
echo "----- remote build log -----"
if [ "$CONCLUSION" = "success" ]; then
    gh run view "$RUN_ID" --repo "$REMOTE_BUILD_REPO" --log 2>/dev/null \
        || echo "(Could not fetch logs. See: ${RUN_URL:-$RUN_ID})"
else
    gh run view "$RUN_ID" --repo "$REMOTE_BUILD_REPO" --log-failed 2>/dev/null \
        || gh run view "$RUN_ID" --repo "$REMOTE_BUILD_REPO" --log 2>/dev/null \
        || echo "(Could not fetch logs. See: ${RUN_URL:-$RUN_ID})"
fi
echo "----- end remote build log -----"
echo ""

# -------------------------------------------------------------- [5/6] result

step "5/6" "Result..."

if [ "$CONCLUSION" != "success" ]; then
    BUILD_FAILED=1
    echo ""
    echo "Build failed."
    [ -n "$RUN_URL" ] && info "Details: $RUN_URL"
    exit 1
fi

# Success: optionally fetch the binary artifact (cargo check
# produces none, which is fine and must not fail the build).
if [ "$REMOTE_BUILD_DOWNLOAD_BINARY" = "1" ]; then
    OUT_SUBDIR="${REMOTE_BUILD_OUTPUT_DIR}/remote-build-${UNIQUE_SUFFIX}"
    mkdir -p "$OUT_SUBDIR"
    info "Downloading binary artifact to ${OUT_SUBDIR}/..."
    if gh run download "$RUN_ID" --repo "$REMOTE_BUILD_REPO" --dir "$OUT_SUBDIR" >/dev/null 2>&1; then
        info "Downloaded:"
        ls -la "$OUT_SUBDIR"
        # Best effort: delete the temporary artifact after download.
        ART_IDS="$(gh api "repos/${REMOTE_BUILD_REPO}/actions/runs/${RUN_ID}/artifacts" \
            --jq '.artifacts[].id' 2>/dev/null || true)"
        for art in $ART_IDS; do
            gh api --silent -X DELETE \
                "repos/${REMOTE_BUILD_REPO}/actions/artifacts/${art}" >/dev/null 2>&1 || true
        done
    else
        info "No binary artifact (expected for 'cargo check' or library crates)."
        rmdir "$OUT_SUBDIR" 2>/dev/null || true
    fi
else
    info "Binary download disabled (REMOTE_BUILD_DOWNLOAD_BINARY=0)."
    [ -n "$RUN_URL" ] && info "Artifact (retention 1 day): $RUN_URL"
fi

# ------------------------------------------------------------- [6/6] cleanup
# Local temp files + remote branch are removed by the EXIT trap.

step "6/6" "Cleaning up..."

echo ""
echo "Build succeeded."
exit 0
