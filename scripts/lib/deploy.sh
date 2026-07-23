#!/usr/bin/env bash
# Remote repository update. Runs git commands ON THE PI over SSH.
#
# Non-destructive by contract: this never runs reset --hard, clean, forced
# checkout, or forced pull. If the remote worktree has local work, deployment
# aborts and asks the user to deal with it. Losing work done directly on a Pi
# is much more expensive than a failed deploy.
#
# DEPLOY_MODE is a seam for a future rsync mode; only "git" is implemented.

[[ -n "${_VA_DEPLOY_SH:-}" ]] && return 0
_VA_DEPLOY_SH=1

DEPLOY_MODE="${DEPLOY_MODE:-git}"

deploy_to_target() {
  case "${DEPLOY_MODE}" in
    git) _deploy_via_git ;;
    rsync)
      die "DEPLOY_MODE=rsync is not implemented yet." \
        "Only the git pull mode exists. Use DEPLOY_MODE=git (the default)."
      ;;
    *)
      die "Unknown DEPLOY_MODE '${DEPLOY_MODE}' (expected 'git')."
      ;;
  esac
}

# Confirm the remote path exists and is a git worktree before touching anything.
_deploy_preflight() {
  local repo status
  repo="$(shq "${TARGET_REMOTE_REPO}")"

  status="$(ssh_run "
    if [ ! -d ${repo} ]; then echo MISSING_DIR; exit 0; fi
    cd ${repo} || { echo NO_CD; exit 0; }
    if ! git rev-parse --git-dir >/dev/null 2>&1; then echo NOT_A_REPO; exit 0; fi
    echo OK
  ")" || die "Could not inspect the remote repository over SSH."

  case "${status}" in
    OK) return 0 ;;
    MISSING_DIR)
      die "The remote repository does not exist on ${TARGET_NAME}: ${TARGET_REMOTE_REPO}" \
        "Nothing has been changed on the Pi." \
        "" \
        "Clone it once, ON THE ${TARGET_PREFIX} (not on the Mac):" \
        "" \
        "    ssh ${TARGET_SSH_HOST}" \
        "    git clone <repo-url> ${TARGET_REMOTE_REPO}" \
        "" \
        "Then install the endpoint service -- see" \
        "docs/raspberry-pi-development-workflow.md." \
        "" \
        "To run the rest of the workflow meanwhile, pass --skip-pull --skip-app."
      ;;
    NOT_A_REPO)
      die "${TARGET_REMOTE_REPO} exists on ${TARGET_NAME} but is not a git repository." \
        "Refusing to touch it -- it may contain files you care about." \
        "Inspect it yourself:  ssh ${TARGET_SSH_HOST} 'ls -la ${TARGET_REMOTE_REPO}'"
      ;;
    *)
      die "Unexpected remote repository state: ${status}"
      ;;
  esac
}

_deploy_via_git() {
  log_step "Updating repository on ${TARGET_NAME} (git pull --ff-only)"
  log_info "Path   : ${TARGET_REMOTE_REPO}"
  log_info "Branch : ${TARGET_BRANCH}"

  _deploy_preflight

  # Refuse to proceed if the remote worktree is dirty. Checked separately from
  # the pull so the message can name the offending files.
  local dirty
  dirty="$(ssh_run "cd $(shq "${TARGET_REMOTE_REPO}") && git status --porcelain")" \
    || die "Could not read git status on ${TARGET_NAME}."

  if [[ -n "${dirty}" ]]; then
    die "The repository on ${TARGET_NAME} has uncommitted or untracked changes." \
      "Deployment aborted. Nothing was modified or discarded." \
      "" \
      "Changed files:" \
      "$(printf '%s\n' "${dirty}" | sed 's/^/  /')" \
      "" \
      "Resolve it ON THE ${TARGET_PREFIX}, choosing what the work is worth:" \
      "" \
      "    ssh ${TARGET_SSH_HOST}" \
      "    cd ${TARGET_REMOTE_REPO}" \
      "    git diff                 # review" \
      "    git stash push -u        # keep it, set it aside" \
      "    # or commit and push it" \
      "" \
      "This launcher never discards remote changes for you."
  fi
  log_ok "Remote worktree is clean"

  if ! would "fetch origin and fast-forward '${TARGET_BRANCH}' on ${TARGET_NAME}"; then
    return 0
  fi

  local out
  if ! out="$(ssh_run "
    set -e
    cd $(shq "${TARGET_REMOTE_REPO}")
    git fetch --prune origin
    git checkout $(shq "${TARGET_BRANCH}")
    git pull --ff-only origin $(shq "${TARGET_BRANCH}")
  " 2>&1)"; then
    die "Failed to update the repository on ${TARGET_NAME}." \
      "$(printf '%s\n' "${out}" | sed 's/^/  /')" \
      "" \
      "Common causes:" \
      "  - No route to GitHub. The Pi Zero's hotspot may have no internet." \
      "    Check:  ssh ${TARGET_SSH_HOST} 'git ls-remote origin'" \
      "  - The branch diverged and cannot fast-forward. Reconcile it manually" \
      "    on the Pi; this launcher will not force anything." \
      "  - Missing credentials for a private repo (use a deploy key or SSH remote)." \
      "" \
      "To continue without deploying, rerun with --skip-pull."
  fi

  printf '%s\n' "${out}" | sed 's/^/    /' >&2

  local head
  head="$(ssh_run "cd $(shq "${TARGET_REMOTE_REPO}") && git log --oneline -1")" || true
  log_ok "Now at: ${head}"
}
