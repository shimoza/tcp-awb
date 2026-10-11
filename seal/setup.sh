#!/usr/bin/env bash
# Seal the Architect Workbench on this host (INTERFACES.md, section "The seal"). Run by the owner with sudo:
#
#   sudo seal/setup.sh [--dry-run] [--mirrors DIR...]                     the first seal of a host, every step
#   releases/<commit>/seal/setup.sh --update [--units-only] [--dry-run] [--mirrors DIR...]
#
# --update is what `sudo awb deploy` runs, from the release folder it has just extracted, never from a working
# tree. Its steps are an explicit allow list (update_steps): the code, the host file, the units, the needrestart
# rule, the package holds of cloudflared and terraform, the egress filter, the client files and the managed drop-in. The users, the homes, the moves, the ssh keys and (without
# --mirrors) the mirrors are left alone, so a deploy never chowns the shared tree again. --units-only renders and
# installs the unit templates and reloads systemd, nothing else (the flip back of a failed reload uses it).
#
# The code lives in one folder per commit, $OPT/releases/<commit>, and $OPT/src is a symlink to the live one. The
# flip is one renameat2(RENAME_EXCHANGE) of a new symlink with whatever src is, a symlink or the plain folder of
# before T1, which then moves to releases/pre-deploy. src resolves to a full tree at every instant; nothing ever
# removes it. The venv finds the package through one .pth line, $OPT/src; pip does not run.
#
# Two users. The owner (the user who runs sudo) keeps the vault. The work user awb has no sudo, is in none of the
# groups of the owner or of the host admins. It runs every working session. Its home holds tcp-shared, tcp-kb and
# the projects. The code is installed read-only to /opt/tcp-awb, the vault daemon runs as the owner. The command
# /usr/local/bin/awb is a root owned wrapper that runs the installed interpreter isolated (python3 -I): no package
# of the invoking user's site folder, PYTHONPATH or working folder is loaded ahead of the installed code.
#
# The client files of the work user are root owned and immutable; the six hooks are also installed as managed
# settings of the client (a drop-in under /etc/claude-code), which no user or project file can switch off.
#
# The build side of the self-build (presentations/self-build, SB1), in the first seal and in every update: the build
# user awb-build (a system user, its own group alone, no password, a login shell for one `claude login`), its folder
# /srv/awb-build on a loop image of a fixed size mounted by an fstab line (a runaway task fills the image, never the
# filesystem of the vault), on it main.git (the owner's, bare, 755), wt/ (the build user's clones), runs/ (the
# runner's records, owner 700), home/ (the build user's own home), venv/ (the suite's venv, root) and home-template/
# (root, immutable: the build settings, CLAUDE.md, the owner's git identity); /opt/claude-build, a root owned copy of
# the client pinned by version and digest with its updater off; the unit of the runner, installed and not enabled
# (the steering session starts it by hand for the first trial task and enables it on his word); the oneshot
# awb-task-failed@.service that every task unit names in OnFailure= (SB5), so a task unit that fails while no runner
# waits on it writes one board line. /etc/awb and its two files go to root:awb (751, paths.conf and blocklist.txt
# 640): the build user reads neither, the owner, the work user and the Ask page do.
#
# The egress filter of the work user (seal/egress/ALLOW.md) is an nftables table that root loads with `awb egress
# load`, at boot through awb-egress.service and every 5 minutes through awb-egress.timer. It ships in the mode count
# (egress = count in the host file: nothing refused, every connection outside the list counted by class); only
# `sudo awb egress enforce` turns it to enforce, and a rewrite of the host file keeps the mode it finds.
#
# Every command is printed before it runs. --dry-run prints every command and runs none (it works without root).
# The script is idempotent: a second run leaves what is in place as it is. A step that would touch the owner's
# vault or password store is refused before anything changes; the only exception is setting their mode.
# Home folders come from getent passwd.
set -euo pipefail

WORK_USER=awb
WORK_GROUP=awb
OPT=/opt/tcp-awb
BIN_LINK=/usr/local/bin/awb
CONF_DIR=/etc/awb
CONF_FILE=/etc/awb/paths.conf
UNITS_DIR=/etc/systemd/system
PRE_DEPLOY=pre-deploy
ROOT_UID=0
NEEDRESTART_FILE=/etc/needrestart/conf.d/awb.conf
CHECK_SOCKET=/run/awb/check.sock
MIRROR_ROOT=/srv/tcp-mirrors
MANAGED_DIR=/etc/claude-code/managed-settings.d
MANAGED_FILE=/etc/claude-code/managed-settings.d/awb-workbench.json
CLIENT_FILES="settings.json CLAUDE.md skills/drafting/SKILL.md"
FORBIDDEN_GROUPS=(ubuntu docker adm sudo lxd)
BUILD_USER=awb-build
BUILD_ROOT=/srv/awb-build             # awb/config.py BUILD_ROOT holds the same path
BUILD_IMAGE=/var/lib/awb-build.img
BUILD_SIZE_GB=20                      # D-S17
BUILD_FLOOR_GB=10                     # the image is made only when / keeps this much free after it
BUILD_FSTAB="$BUILD_IMAGE $BUILD_ROOT ext4 loop,nofail,nodev,nosuid,noatime 0 0"
FSTAB=/etc/fstab
BUILD_CLIENT=/opt/claude-build
BUILD_CLIENT_VERSION=2.1.296          # the client the build user runs, pinned by version and digest
BUILD_CLIENT_SHA256=24972e3bc859fab2b46ed4c1e51f7d6130f06d3bd550811a114640de3370d0de
INSTALL_ONLY="awb-buildd.service"     # installed, never enabled here: the steering session starts it by hand
BUILD_FAILED_UNIT=awb-task-failed@.service   # OnFailure= of every task unit (awb/buildrun.py FAILED_UNIT, SB5)
BUILD_MODEL_DEFAULT=login             # D-S7 changed: his subscription login; build_model = key in the host file is the fallback
BUILD_MODEL_KEY=/etc/awb/build-model.key     # the owner's copy for the runner (build_model = key), never printed
BUILD_MODEL_KEY_SOURCE=/etc/awb/anthropic.key   # the default source; build_model_key_source in the host file wins
BUILD_KEY_HELPER='  "apiKeyHelper": "cat \"$HOME/.config/awb-build/model.key\"",'   # the template's line with build_model = key

usage() {
    echo "usage: sudo $0 [--dry-run] [--update [--units-only]] [--mirrors DIR...]" >&2
    echo "       $0 [--dry-run] --suite-venv DIR" >&2
    exit 2
}

die() {
    echo "setup: $*" >&2
    exit 1
}

dry_run=0
real=1   # 0 for --dry-run; stays 1 in the silent plan pass of a real run, where checks of the host still apply
mirrors=0
mirror_sources=()
update=0
units_only=0
suite_dir=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            dry_run=1
            real=0
            ;;
        --update) update=1 ;;
        --units-only) units_only=1 ;;
        --mirrors) mirrors=1 ;;
        --suite-venv)
            [ $# -ge 2 ] || usage
            suite_dir="$2"
            shift
            ;;
        -h | --help)
            echo "usage: sudo $0 [--dry-run] [--update [--units-only]] [--mirrors DIR...]"
            echo "       $0 [--dry-run] --suite-venv DIR"
            exit 0
            ;;
        -*) usage ;;
        *)
            [ "$mirrors" -eq 1 ] || usage
            mirror_sources+=("$1")
            ;;
    esac
    shift
done
if [ "$mirrors" -eq 1 ] && [ "${#mirror_sources[@]}" -eq 0 ]; then
    die "--mirrors needs at least one folder"
fi
[ "$units_only" -eq 0 ] || [ "$update" -eq 1 ] || die "--units-only goes with --update"

# AWB_SETUP_BUILD_HOST=fresh|present, with --dry-run only (the tests set it): every probe of the build side (the
# build user's group, its passwd entry and groups, the suite's venv) answers "missing" or "present" instead of
# looking at this host, so a plan does not depend on the host it is printed on. Unset, the probes look at the host.
build_host="${AWB_SETUP_BUILD_HOST:-}"
case "$build_host" in
    "") ;;
    fresh | present) [ "$dry_run" -eq 1 ] || die "AWB_SETUP_BUILD_HOST goes with --dry-run only" ;;
    *) die "AWB_SETUP_BUILD_HOST is fresh or present" ;;
esac
# AWB_SETUP_BUILD_MODEL=login|key, with --dry-run only (the tests set it): the build model of the plan instead of
# build_model of the host file this host carries.
case "${AWB_SETUP_BUILD_MODEL:-}" in
    "") ;;
    login | key) [ "$dry_run" -eq 1 ] || die "AWB_SETUP_BUILD_MODEL goes with --dry-run only" ;;
    *) die "AWB_SETUP_BUILD_MODEL is login or key" ;;
esac

# --------------------------------------------------------------------------- the venv of the suite

# suite_venv DIR: the venv the whole suite runs with from a read-only export of the tree (presentations/self-build,
# SB0): python3 -m venv --system-site-packages and the test extras of pyproject.toml, nothing else. The package
# itself is never installed: the suite imports it from the tree it runs in, by the interpreter's absolute path with
# python -I -B. DIR is an absolute path outside every home and outside the repository. SB1 calls it as root for
# /srv/awb-build/venv; `--suite-venv DIR` runs it alone, as any user, and touches nothing else of the host.
suite_venv() {
    local dir="$1" here extras
    here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
    case "$dir" in
        /?*) ;;
        *) die "--suite-venv needs an absolute path" ;;
    esac
    case "$dir" in
        *..* | "$here" | "$here"/* | "${HOME:-/nonexistent}" | "${HOME:-/nonexistent}"/*) die "the venv of the suite lies outside every home and outside the repository: $dir" ;;
    esac
    # the homes of root and of every person (uid 1000 and up) from getent passwd
    while IFS=: read -r _ _ uid _ _ h _; do
        { [ "$uid" -eq 0 ] || [ "$uid" -ge 1000 ]; } && [ "${h%/}" != "" ] || continue
        case "$dir" in
            "${h%/}" | "${h%/}"/*) die "the venv of the suite lies outside every home and outside the repository: $dir" ;;
        esac
    done < <(getent passwd)
    extras=$(/usr/bin/python3 -I -c 'import sys, tomllib
with open(sys.argv[1], "rb") as f:
    print(" ".join(tomllib.load(f)["project"]["optional-dependencies"]["test"]))' "$here/pyproject.toml") ||
        die "the test extras of pyproject.toml could not be read"
    printf '# the venv of the suite in %s: the system packages and the test extras, not the package\n' "$dir"
    if { [ -z "$build_host" ] && [ -e "$dir/bin/python" ]; } || [ "$build_host" = present ]; then
        printf '= python3 -m venv --system-site-packages %s  # already there\n' "$dir"
    else
        printf '+ python3 -m venv --system-site-packages %s\n' "$dir"
        [ "$dry_run" -eq 1 ] || python3 -m venv --system-site-packages "$dir"
    fi
    printf '+ %s/bin/python -I -m pip install --no-cache-dir --disable-pip-version-check %s\n' "$dir" "$extras"
    # shellcheck disable=SC2086  # the extras are words without blanks, one per requirement
    [ "$dry_run" -eq 1 ] || "$dir/bin/python" -I -m pip install --no-cache-dir --disable-pip-version-check \
        --quiet $extras
}

if [ -n "$suite_dir" ]; then
    [ "$update" -eq 0 ] && [ "$mirrors" -eq 0 ] || die "--suite-venv goes alone (with --dry-run at most)"
    suite_venv "$suite_dir"
    exit 0
fi

# a test-only install root and bin link, so that the code step can run for real under pytest: never as root,
# only the code step runs and git is called directly
test_root=0
if [ -n "${AWB_SETUP_OPT:-}" ] || [ -n "${AWB_SETUP_BIN:-}" ]; then
    [ "$(id -u)" -ne 0 ] || die "AWB_SETUP_OPT and AWB_SETUP_BIN are for the tests and refused as root"
    [ -n "${AWB_SETUP_OPT:-}" ] && [ -n "${AWB_SETUP_BIN:-}" ] || die "the tests set AWB_SETUP_OPT and AWB_SETUP_BIN"
    OPT=$AWB_SETUP_OPT
    BIN_LINK=$AWB_SETUP_BIN
    test_root=1
fi

# --------------------------------------------------------------------------- who is who

owner="${SUDO_USER:-}"
[ -n "$owner" ] || die "run this with sudo as the owner (SUDO_USER is empty)"
[ "$owner" != root ] || die "the owner must be a user, not root"
[ "$owner" != "$WORK_USER" ] || die "the owner must not be the work user"
if [ "$dry_run" -eq 0 ] && [ "$(id -u)" -ne 0 ] && [ "$test_root" -eq 0 ]; then
    die "run this with sudo or use --dry-run"
fi

owner_entry=$(getent passwd "$owner") || die "the owner is not a user of this host"
owner_home=$(printf '%s\n' "$owner_entry" | cut -d: -f6)
owner_gid=$(printf '%s\n' "$owner_entry" | cut -d: -f4)
owner_group=$(getent group "$owner_gid" | cut -d: -f1) || owner_group=""
case "$owner_home" in
    /?*) owner_home="${owner_home%/}" ;;
    *) die "the owner has no home folder" ;;
esac
forbidden=("${FORBIDDEN_GROUPS[@]}")
if [ -n "$owner_group" ] && [[ " ${forbidden[*]} " != *" $owner_group "* ]]; then
    forbidden+=("$owner_group")
fi

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
seal_dir="$repo/seal"
vault="$owner_home/tcp-vault"
pstore="$owner_home/.password-store"
protected=("$vault" "$pstore")

work_exists=0
work_home="<work-home>"
if work_entry=$(getent passwd "$WORK_USER"); then
    work_exists=1
    work_home=$(printf '%s\n' "$work_entry" | cut -d: -f6)
    [ "$(printf '%s\n' "$work_entry" | cut -d: -f3)" != 0 ] || die "the work user must not have uid 0"
fi

# --------------------------------------------------------------------------- printing and the guard

SAFE_WORD='^[A-Za-z0-9_./:=@%+,<>|*-]+$'

quote_args() {
    local a out=""
    for a in "$@"; do
        if [[ "$a" =~ $SAFE_WORD ]]; then
            out+=" $a"
        else
            out+=" $(printf '%q' "$a")"
        fi
    done
    printf '%s' "${out# }"
}

show() { printf '+ %s\n' "$(quote_args "$@")"; }

skip() {
    local why="$1"
    shift
    printf '= %s  # %s\n' "$(quote_args "$@")" "$why"
}

info() { printf '# %s\n' "$*"; }

inside() {
    # inside PATH BASE: true when PATH is BASE or lies under it
    case "$1" in
        "$2" | "$2"/*) return 0 ;;
    esac
    return 1
}

guard() {
    # guard CMD ARG...: refuse a step that would touch the owner's vault or password store. Setting the mode of
    # the vault or the password store itself (chmod, not recursive) is the only exception.
    local cmd="$1" a q recursive=0
    shift
    for a in "$@"; do
        case "$a" in
            -R | -r | -a | -rf | -fr | --recursive) recursive=1 ;;
        esac
    done
    for a in "$@"; do
        for q in "${protected[@]}"; do
            if inside "$a" "$q"; then
                if [ "$cmd" = chmod ] && [ "$recursive" -eq 0 ] && [ "$a" = "$q" ]; then
                    continue
                fi
                die "refused: a step would touch the owner's vault or password store, nothing was changed by it"
            fi
            if [ "$recursive" -eq 1 ] && inside "$q" "${a%/}"; then
                die "refused: a recursive step would reach the owner's vault or password store"
            fi
        done
    done
}

run() {
    guard "$@"
    show "$@"
    [ "$dry_run" -eq 1 ] || "$@"
}

try_run() {
    # try_run CMD...: like run, but a failure only warns (seal/verify.sh reports what it left undone)
    guard "$@"
    show "$@"
    [ "$dry_run" -eq 0 ] || return 0
    "$@" || info "warning: the step above failed, seal/verify.sh will report it"
}

run_if_present() {
    # run_if_present PATH CMD...: in a real run only when PATH exists
    local path="$1"
    shift
    guard "$@"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ %s  # if present\n' "$(quote_args "$@")"
    elif [ -e "$path" ]; then
        show "$@"
        "$@"
    else
        skip "not present" "$@"
    fi
}

run_unless_present() {
    # run_unless_present PATH CMD...: in a real run only when PATH does not exist yet
    local path="$1"
    shift
    guard "$@"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ %s  # if missing\n' "$(quote_args "$@")"
    elif [ -e "$path" ]; then
        skip "already there" "$@"
    else
        show "$@"
        "$@"
    fi
}

write_file() {
    # write_file TARGET MODE OWNER:GROUP, the content on standard input; atomic in a real run
    local target="$1" mode="$2" og="$3" content tmp
    content=$(cat)
    guard install "$target"
    printf '+ write %s (mode %s, %s)\n' "$target" "$mode" "$og"
    printf '%s\n' "$content" | sed 's/^/    | /'
    [ "$dry_run" -eq 0 ] || return 0
    tmp=$(mktemp "$(dirname -- "$target")/.awb-setup.XXXXXX")
    printf '%s\n' "$content" >"$tmp"
    [ "$test_root" -eq 1 ] || chown "$og" "$tmp"
    chmod "$mode" "$tmp"
    mv -f -- "$tmp" "$target"
}

append_line() {
    # append_line FILE LINE: append LINE once
    local file="$1" line="$2"
    guard tee "$file"
    if [ "$dry_run" -eq 0 ] && grep -qxF -- "$line" "$file" 2>/dev/null; then
        printf '= append to %s: %s  # already there\n' "$file" "$line"
        return 0
    fi
    printf '+ append to %s: %s\n' "$file" "$line"
    [ "$dry_run" -eq 1 ] || printf '%s\n' "$line" >>"$file"
}

in_group() {
    # in_group USER GROUP: USER is a member of GROUP (listed or as its primary group)
    local user="$1" group="$2" entry m gid pgid
    entry=$(getent group "$group") || return 1
    for m in $(printf '%s\n' "$entry" | cut -d: -f4 | tr ',' ' '); do
        [ "$m" = "$user" ] && return 0
    done
    gid=$(printf '%s\n' "$entry" | cut -d: -f3)
    pgid=$(getent passwd "$user" | cut -d: -f4) || return 1
    [ -n "$pgid" ] && [ "$pgid" = "$gid" ]
}

# --------------------------------------------------------------------------- the steps

step_users() {
    info "the work user and its group"
    if getent group "$WORK_GROUP" >/dev/null; then
        skip "the group exists" groupadd "$WORK_GROUP"
    else
        run groupadd "$WORK_GROUP"
    fi
    if [ "$work_exists" -eq 1 ]; then
        skip "the user exists" useradd --create-home --gid "$WORK_GROUP" --shell /bin/bash "$WORK_USER"
    else
        run useradd --create-home --gid "$WORK_GROUP" --shell /bin/bash --comment "Architect Workbench sessions" \
            "$WORK_USER"
        if [ "$dry_run" -eq 0 ]; then
            work_home=$(getent passwd "$WORK_USER" | cut -d: -f6)
        fi
    fi
    if [ "$work_home" != "<work-home>" ]; then
        case "$work_home" in
            /?*) work_home="${work_home%/}" ;;
            *) die "the work user has no home folder" ;;
        esac
        if inside "$work_home" "$owner_home" || inside "$owner_home" "$work_home"; then
            die "the homes of the owner and the work user must lie apart"
        fi
    fi

    info "the work user is in none of the groups ${forbidden[*]}"
    local g
    if [ "$work_exists" -eq 1 ] || [ "$dry_run" -eq 0 ]; then
        for g in "${forbidden[@]}"; do
            if in_group "$WORK_USER" "$g"; then
                run gpasswd -d "$WORK_USER" "$g"
            fi
        done
    else
        info "a new work user is in the group $WORK_GROUP only"
    fi

    info "the service user of the Ask page (awb-ask.service): no login, no home, the group $WORK_GROUP"
    if getent passwd awb-ask >/dev/null; then
        skip "the user exists" useradd --system --gid "$WORK_GROUP" --no-create-home -d /nonexistent \
            --shell /usr/sbin/nologin awb-ask
    else
        run useradd --system --gid "$WORK_GROUP" --no-create-home -d /nonexistent --shell /usr/sbin/nologin awb-ask
    fi

    info "the owner joins the group $WORK_GROUP, so that the intake can write the outbox"
    if in_group "$owner" "$WORK_GROUP"; then
        skip "already a member" usermod -a -G "$WORK_GROUP" "$owner"
    else
        run usermod -a -G "$WORK_GROUP" "$owner"
    fi
}

step_homes() {
    info "home modes: the work user cannot enter the owner's home"
    run chmod 750 "$owner_home"
    run chown "$WORK_USER:$WORK_GROUP" "$work_home"
    run chmod 750 "$work_home"
    run_if_present "$vault" chmod 700 "$vault"
    run_if_present "$pstore" chmod 700 "$pstore"
}

move_folder() {
    # move_folder NAME: <owner-home>/NAME to <work-home>/NAME
    local src="$owner_home/$1" dst="$work_home/$1"
    if [ "$dry_run" -eq 1 ]; then
        run_if_present "$src" mv -- "$src" "$dst"
        return 0
    fi
    if [ -e "$src" ] && [ -e "$dst" ]; then
        die "both $1 folders exist, merge them by hand and run again"
    fi
    run_if_present "$src" mv -- "$src" "$dst"
}

rewrite_project_paths() {
    # the project register names every project by its absolute path; moved projects get their new path
    local reg="$1" tmp
    guard awk "$reg"
    printf '+ rewrite %s: project paths under %s point to %s\n' "$reg" "$owner_home" "$work_home"
    [ "$dry_run" -eq 0 ] || return 0
    tmp=$(mktemp "$(dirname -- "$reg")/.projects.XXXXXX")
    awk -F '\t' -v OFS='\t' -v old="$owner_home/" -v new="$work_home/" '
        NR > 1 && index($5, old) == 1 { $5 = new substr($5, length(old) + 1); m = $5; gsub("/", "-", m); $6 = m }
        { print }' "$reg" >"$tmp"
    chown "$WORK_USER:$WORK_GROUP" "$tmp"
    chmod 640 "$tmp"
    mv -f -- "$tmp" "$reg"
}

move_projects() {
    local shared="$1" reg="$1/projects.tsv" code kind customer platform path rest
    if [ "$dry_run" -eq 1 ]; then
        printf '+ mv -- %s/tcp-<code> %s/tcp-<code>  # for each project of projects.tsv in the owner'"'"'s home\n' \
            "$owner_home" "$work_home"
        printf '+ chown -R %s:%s %s/tcp-<code>  # for each moved project\n' "$WORK_USER" "$WORK_GROUP" "$work_home"
        rewrite_project_paths "$reg"
        return 0
    fi
    if [ ! -f "$reg" ]; then
        info "no project register yet, no project to move"
        return 0
    fi
    while IFS=$'\t' read -r code kind customer platform path rest; do
        [ "$code" != code ] || continue
        [[ "$code" =~ ^(tcp|hcs)-[a-z2-7]{4}$ ]] || die "projects.tsv holds a line without a project code"
        [ "$path" = "$owner_home/$code" ] || continue
        if [ -e "$path" ] && [ -e "$work_home/$code" ]; then
            die "project $code exists in both homes, merge it by hand and run again"
        fi
        run_if_present "$path" mv -- "$path" "$work_home/$code"
        run_if_present "$work_home/$code" chown -R "$WORK_USER:$WORK_GROUP" "$work_home/$code"
    done <"$reg"
    if grep -qF -- "$(printf '\t')$owner_home/" "$reg"; then
        rewrite_project_paths "$reg"
    fi
}

step_moves() {
    local shared="$work_home/tcp-shared" kb="$work_home/tcp-kb"
    info "the shared side, the knowledge base and the projects move to the work user"
    move_folder tcp-shared
    run mkdir -p "$shared/outbox" "$shared/ledger" "$shared/tenants"
    run chown -R "$WORK_USER:$WORK_GROUP" "$shared"
    run chmod 2750 "$shared"
    run chmod -R u+rwX,g+rwX,o-rwx "$shared/outbox" "$shared/tenants"
    # setgid keeps the group, the sticky bit keeps one user from renaming or removing another user's entries
    run find "$shared/outbox" "$shared/tenants" -type d -exec chmod 3770 {} +
    move_projects "$shared"
    move_folder tcp-kb
    run mkdir -p "$kb"
    run chown -R "$WORK_USER:$WORK_GROUP" "$kb"
    run chmod 750 "$kb"
    run runuser -u "$WORK_USER" -- env HOME="$work_home" git -C "$kb" init -q
}

# renameat2(RENAME_EXCHANGE) through ctypes: swaps the two names in one call, a symlink with a symlink or with a
# folder, so the path resolves to a full tree at every instant
EXCHANGE_PY='import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
if libc.renameat2(-100, os.fsencode(sys.argv[1]), -100, os.fsencode(sys.argv[2]), 2) != 0:
    sys.exit("exchange: " + os.strerror(ctypes.get_errno()))'

exchange() {
    guard python3 "$1" "$2"
    printf '+ exchange %s %s  # renameat2 RENAME_EXCHANGE, one call\n' "$1" "$2"
    [ "$dry_run" -eq 1 ] || python3 -I -c "$EXCHANGE_PY" "$1" "$2"
}

git_repo() {
    # git in the working tree runs as the owner (git refuses a repository of another user); the tests call it
    # directly
    if [ "$test_root" -eq 1 ]; then
        git -C "$repo" "$@"
    else
        runuser -u "$owner" -- env HOME="$owner_home" git -C "$repo" "$@"
    fi
}

archive_release() {
    # the full mode: HEAD of the working tree into releases/<commit>, through a .partial name; a complete folder
    # of that commit is reused
    local commit partial target
    if [ "$dry_run" -eq 1 ]; then
        release="<HEAD>"
        partial="$OPT/releases/.$release.partial"
        printf '+ %s | %s\n' "$(quote_args runuser -u "$owner" -- env HOME="$owner_home" git -C "$repo" archive HEAD)" \
            "$(quote_args tar -x --no-same-owner --no-same-permissions -C "$partial")"
        run chmod -R go-w "$partial"
        run mv -T -- "$partial" "$OPT/releases/$release"
        return 0
    fi
    commit=$(git_repo rev-parse --verify 'HEAD^{commit}') || die "git cannot read HEAD of the repository"
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || die "git gave no commit id for HEAD"
    release=$commit
    target="$OPT/releases/$commit"
    if [ -d "$target" ] && [ ! -L "$target" ]; then
        skip "complete from an earlier run" mv -T -- "$OPT/releases/.$commit.partial" "$target"
        return 0
    fi
    partial="$OPT/releases/.$commit.partial"
    run_if_present "$partial" rm -rf -- "$partial"
    run mkdir -p "$partial"
    printf '+ %s | %s\n' "$(quote_args git -C "$repo" archive "$commit")" \
        "$(quote_args tar -x --no-same-owner --no-same-permissions -C "$partial")"
    git_repo archive "$commit" | tar -x --no-same-owner --no-same-permissions -C "$partial"
    run chmod -R go-w "$partial"
    run mv -T -- "$partial" "$target"
}

release_of_repo() {
    # the update mode: the script runs from the release folder awb deploy extracted; its name is the release
    release=$(basename -- "$repo")
    if [ "$(dirname -- "$repo")" = "$(cd -- "$OPT/releases" 2>/dev/null && pwd -P)" ] &&
        [[ "$release" =~ ^([0-9a-f]{40}|$PRE_DEPLOY)$ ]]; then
        return 0
    fi
    if [ "$dry_run" -eq 1 ]; then
        release="<commit>"
        info "this copy lies outside $OPT/releases: the release is printed as $release"
        return 0
    fi
    die "setup.sh --update runs from a release folder in $OPT/releases (sudo awb deploy does it)"
}

site_dir() {
    local d
    for d in "$OPT"/venv/lib/python3*/site-packages; do
        if [ -d "$d" ]; then
            printf '%s' "$d"
            return 0
        fi
    done
    printf '%s' "$OPT/venv/lib/python3/site-packages"
}

remove_editable() {
    # the three files of the editable pip install of before T1, removed after the .pth line is in place
    local site="$1" f
    if [ "$dry_run" -eq 1 ]; then
        printf '+ rm -rf -- %s/__editable__*tcp_awb* %s/tcp_awb-*.dist-info  # if present\n' "$site" "$site"
        return 0
    fi
    shopt -s nullglob
    for f in "$site"/__editable__*tcp_awb* "$site"/tcp_awb-*.dist-info; do
        run rm -rf -- "$f"
    done
    shopt -u nullglob
}

migrate_plain() {
    # migrate_plain PATH: the plain folder of before T1, swapped out to PATH, moves to releases/pre-deploy; an old
    # symlink at PATH is removed
    local folder="$1" dst="$OPT/releases/$PRE_DEPLOY"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ mv -T -- %s %s  # if %s is the plain folder of before T1, else rm -f\n' "$folder" "$dst" "$folder"
        return 0
    fi
    if [ -d "$folder" ] && [ ! -L "$folder" ]; then
        [ ! -e "$dst" ] || dst="$OPT/releases/$PRE_DEPLOY.$(date +%s)"
        run mv -T -- "$folder" "$dst"
    elif [ -L "$folder" ]; then
        run rm -f -- "$folder"
    fi
}

flip_to() {
    # flip_to RELEASE: src points at releases/RELEASE after one exchange. The exchange is the flip; what follows
    # only tidies the old name
    local release="$1" next="$OPT/src.next" src="$OPT/src"
    if [ "$dry_run" -eq 0 ] && [ -d "$next" ] && [ ! -L "$next" ]; then
        migrate_plain "$next"   # a plain folder an interrupted first run left at src.next
    fi
    run ln -sfn "releases/$release" "$next"
    if [ "$dry_run" -eq 1 ] || [ -e "$src" ] || [ -L "$src" ]; then
        exchange "$next" "$src"
    else
        run mv -T -- "$next" "$src"
    fi
    migrate_plain "$next"
}

step_code() {
    local site
    info "the code: one folder per commit in $OPT/releases, $OPT/src a symlink to the live one, one venv"
    run mkdir -p "$OPT/releases"
    if [ "$update" -eq 1 ]; then
        release_of_repo
    else
        archive_release
    fi
    run_unless_present "$OPT/venv/bin/python" python3 -m venv --system-site-packages "$OPT/venv"
    site=$(site_dir)
    info "the venv finds the package through one line, $OPT/src: written first, the editable install removed last"
    write_file "$site/awb.pth" 644 root:root <<<"$OPT/src"
    remove_editable "$site"
    # a wrapper, not a symlink to the venv script: -I ignores the invoking user's site folder, PYTHONPATH and the
    # working folder, which would otherwise load ahead of the installed code (T3); the AWB_* variables survive
    write_file "$BIN_LINK" 755 root:root <<EOF
#!/bin/sh
# written by seal/setup.sh: the installed Workbench, run isolated from the invoking user's packages
exec $OPT/venv/bin/python3 -I -m awb "\$@"
EOF
    info "the flip, the last step of the code: $OPT/src points at releases/$release"
    flip_to "$release"
}

# kept_conf KEY DEFAULT: the value of KEY in the host file of before, else DEFAULT (the web keys of D-FRONT and
# D-HOST outlive a rewrite: owner_host is his, set once with sudo awb web host HOST)
kept_conf() {
    local value=""
    [ ! -r "$CONF_FILE" ] || value=$(sed -n "s/^$1[[:space:]]*=[[:space:]]*//p" "$CONF_FILE" | head -n 1)
    printf '%s' "${value:-$2}"
}

# build_model_mode: build_model of the host file (login unless it says key; a test's dry run may name it); any
# other value refuses the plan before the first change
build_model_mode() {
    local mode="${AWB_SETUP_BUILD_MODEL:-}"
    [ -n "$mode" ] || mode=$(kept_conf build_model "$BUILD_MODEL_DEFAULT")
    case "$mode" in
        login | key) printf '%s' "$mode" ;;
        *) die "build_model in $CONF_FILE is login or key" ;;
    esac
}

step_conf() {
    local web_keys owner_host
    info "the host file of the Workbench paths: /etc/awb root:awb 751 (a user outside the group may reach a file it"
    info "owns there, awb-owner its secret, and lists nothing), paths.conf and blocklist.txt 640 root:awb"
    run mkdir -p "$CONF_DIR"
    run chown root:"$WORK_GROUP" "$CONF_DIR"
    run chmod 751 "$CONF_DIR"
    owner_host=$(kept_conf owner_host "")
    web_keys="front_socket = $(kept_conf front_socket /run/awb-web/front.sock)
cloudflared_user = $(kept_conf cloudflared_user cloudflared)"
    [ -z "$owner_host" ] || web_keys="$web_keys
owner_host = $owner_host"
    write_file "$CONF_FILE" 640 root:"$WORK_GROUP" <<EOF
# written by seal/setup.sh, read by awb/config.py
owner = $owner
work_user = $WORK_USER
shared = $work_home/tcp-shared
vault = $vault
projects = $work_home
kb = $work_home/tcp-kb
check_socket = $CHECK_SOCKET
mirrors = $MIRROR_ROOT
egress = $(kept_conf egress count)
build_user = $BUILD_USER
build_root = $BUILD_ROOT
build_model = $build_model
build_model_key = $BUILD_MODEL_KEY
build_model_key_source = $(kept_conf build_model_key_source "$BUILD_MODEL_KEY_SOURCE")
build_push = $(kept_conf build_push yes)
$web_keys
EOF
    run_if_present "$CONF_DIR/blocklist.txt" chown root:"$WORK_GROUP" "$CONF_DIR/blocklist.txt"
    run_if_present "$CONF_DIR/blocklist.txt" chmod 640 "$CONF_DIR/blocklist.txt"
}

step_units() {
    local f name
    info "the units of seal/*.service, rendered with the owner: the vault daemon and the key service run as the owner"
    info "with the group $WORK_GROUP, the Ask page as awb-ask (its key file stays the owner's step), the portal as"
    info "the work user. A running unit is left running: sudo awb deploy restarts what changed"
    info "The vault locks on a stop, a crash or a reboot. awb vault reload (what awb deploy uses after a code update)"
    info "hands the passphrase to the new process over a private socket pair and keeps it unlocked"
    for f in "$seal_dir"/*.service "$seal_dir"/*.timer; do
        name=$(basename -- "$f")
        write_file "$UNITS_DIR/$name" 644 root:root < <(sed -e "s|@OWNER@|$owner|g" "$f")
    done
    [ "$units_only" -eq 1 ] || build_failed_unit
    run systemctl daemon-reload
    [ "$units_only" -eq 0 ] || return 0
    for f in "$seal_dir"/*.service "$seal_dir"/*.timer; do
        name=$(basename -- "$f")
        if [[ " $INSTALL_ONLY " == *" $name "* ]]; then
            info "$name is installed, not enabled: the steering session starts it by hand for a trial task and enables it on his word"
            continue
        fi
        try_run systemctl enable --now "$name"
    done
}

step_egress() {
    info "the egress filter of the work user (seal/egress/ALLOW.md): awb egress load resolves the list as root, loads"
    info "the table inet awb_egress in the mode of the host file (count unless the owner ran sudo awb egress enforce)"
    info "or refreshes its address sets in place, and records the counters; awb egress status shows them"
    try_run "$BIN_LINK" egress load
}

step_needrestart() {
    info "needrestart leaves the vault daemon and the key service running after a package upgrade"
    run mkdir -p "$(dirname "$NEEDRESTART_FILE")"
    write_file "$NEEDRESTART_FILE" 644 root:root <"$seal_dir/needrestart-awb.conf"
}

step_holds() {
    local pkg
    info "cloudflared and terraform are held (RT-26): the routine upgrade leaves them alone and an upgrade is a"
    info "deliberate step (apt-mark unhold PACKAGE, upgrade, apt-mark hold PACKAGE); the runner of a project's"
    info "Terraform set uses its own pinned binary, never the one of apt"
    for pkg in cloudflared terraform; do
        if [ "$real" -eq 1 ] && ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
            info "$pkg is not installed: nothing to hold"
            continue
        fi
        if [ "$real" -eq 1 ] && apt-mark showhold "$pkg" 2>/dev/null | grep -qx "$pkg"; then
            printf '= apt-mark hold %s  # already held\n' "$pkg"
            continue
        fi
        try_run apt-mark hold "$pkg"
    done
}

step_kb_hooks() {
    info "the knowledge base gets the commit gate as the work user: pre-commit (with awb kb verify --staged),"
    info "commit-msg and pre-push; seal/verify.sh checks the three are there"
    try_run runuser -u "$WORK_USER" -- env HOME="$work_home" "$BIN_LINK" gate --install --kb \
        --repo "$work_home/tcp-kb"
}

step_ssh() {
    local keys="$owner_home/.ssh/authorized_keys"
    info "the owner's ssh keys open the work user too"
    run mkdir -p "$work_home/.ssh"
    run chown "$WORK_USER:$WORK_GROUP" "$work_home/.ssh"
    run chmod 700 "$work_home/.ssh"
    run_if_present "$keys" install -o "$WORK_USER" -g "$WORK_GROUP" -m 600 "$keys" "$work_home/.ssh/authorized_keys"
}

clear_target() {
    # clear_target PATH f|d: a root owned regular file (f) or folder (d) loses its immutable flag as before;
    # anything else at PATH (a link, a file or folder of the work user, another type) is removed first, so a
    # planted entry can neither turn chattr onto another file nor stop the deploy
    local path="$1" kind="$2"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ chattr -i %s  # if present\n' "$path"
        return 0
    fi
    [ -e "$path" ] || [ -L "$path" ] || return 0
    if [ ! -L "$path" ] && [ "$(stat -c %u -- "$path")" = "$ROOT_UID" ] &&
        { { [ "$kind" = f ] && [ -f "$path" ]; } || { [ "$kind" = d ] && [ -d "$path" ]; }; }; then
        run chattr -i "$path"
    else
        info "a foreign entry at $path (a link, another owner or another type) is removed first"
        run rm -rf -- "$path"
    fi
}

step_client() {
    local f target
    info "the client settings and instructions of the work user: owned by root and immutable (chattr +i). The"
    info "folder stays the work user's own (the client keeps its login and history there), so without the flag"
    info "the work user could rename the files and put its own in their place"
    run mkdir -p "$work_home/.claude"
    run chown "$WORK_USER:$WORK_GROUP" "$work_home/.claude"
    run chmod 700 "$work_home/.claude"
    info "the drafting skill goes to skills/drafting, its folders owned by root and immutable as well, so the work"
    info "user can neither rename the folder nor put a skill of its own in its place"
    for d in skills skills/drafting; do
        clear_target "$work_home/.claude/$d" d
    done
    run install -d -o root -g "$WORK_GROUP" -m 755 "$work_home/.claude/skills" "$work_home/.claude/skills/drafting"
    for f in $CLIENT_FILES; do
        target="$work_home/.claude/$f"
        clear_target "$target" f
        run install -o root -g "$WORK_GROUP" -m 644 "$seal_dir/work-claude/$f" "$target"
        try_run chattr +i "$target"
    done
    for d in skills/drafting skills; do
        try_run chattr +i "$work_home/.claude/$d"
    done
    info "the git identity of the work user (.gitconfig in its home), root owned and immutable, so a plain git"
    info "commit in a project works and names the Workbench"
    clear_target "$work_home/.gitconfig" f
    run install -o root -g "$WORK_GROUP" -m 644 "$seal_dir/work-gitconfig" "$work_home/.gitconfig"
    try_run chattr +i "$work_home/.gitconfig"
}

step_managed() {
    local src="$seal_dir/work-claude/managed-settings.json"
    info "the six hooks as managed settings of the client (a drop-in file): no user or project file can switch"
    info "them off, disableAllHooks included. awb hook exits 0 for every user but work_user of $CONF_FILE, so the"
    info "owner's own sessions are not checked. The deny rule for connectors stays in the work user's settings"
    if [ "$real" -eq 1 ] && [ -e "$MANAGED_FILE" ] && ! cmp -s -- "$src" "$MANAGED_FILE"; then
        info "replacing an older copy of $MANAGED_FILE"
    fi
    run mkdir -p "$MANAGED_DIR"
    run chmod 755 "$MANAGED_DIR"
    run install -o root -g root -m 644 "$src" "$MANAGED_FILE"
}

# --------------------------------------------------------------------------- the build side (self-build SB1)

build_mounted=0
build_source=""

# the probes of the build user, answered by AWB_SETUP_BUILD_HOST in a dry run when it is set
build_group_entry() {
    case "$build_host" in
        fresh) return 1 ;;
        present) printf '%s:x:999:\n' "$BUILD_USER" ;;
        *) getent group "$BUILD_USER" ;;
    esac
}

build_user_entry() {
    case "$build_host" in
        fresh) return 1 ;;
        present) printf '%s:x:999:999:Architect Workbench build:%s/home:/bin/bash\n' "$BUILD_USER" "$BUILD_ROOT" ;;
        *) getent passwd "$BUILD_USER" ;;
    esac
}

build_user_groups() {
    case "$build_host" in
        fresh) return 0 ;;
        present) printf '%s\n' "$BUILD_USER" ;;
        *) id -nG "$BUILD_USER" ;;
    esac
}

step_build_user() {
    local entry home shell g
    info "the build user $BUILD_USER of the self-build: a system user in its own group alone, no password, the shell"
    info "/bin/bash so that claude login works once, its home $BUILD_ROOT/home inside the build root"
    if build_group_entry >/dev/null; then
        skip "the group exists" groupadd --system "$BUILD_USER"
    else
        run groupadd --system "$BUILD_USER"
    fi
    if entry=$(build_user_entry); then
        [ "$(printf '%s\n' "$entry" | cut -d: -f3)" != 0 ] || die "the build user must not have uid 0"
        skip "the user exists" useradd --system --gid "$BUILD_USER" -M -d "$BUILD_ROOT/home" --shell /bin/bash \
            "$BUILD_USER"
        home=$(printf '%s\n' "$entry" | cut -d: -f6)
        shell=$(printf '%s\n' "$entry" | cut -d: -f7)
        [ "$home" = "$BUILD_ROOT/home" ] || run usermod -d "$BUILD_ROOT/home" "$BUILD_USER"
        [ "$shell" = /bin/bash ] || run usermod --shell /bin/bash "$BUILD_USER"
    else
        run useradd --system --gid "$BUILD_USER" -M -d "$BUILD_ROOT/home" --shell /bin/bash \
            --comment "Architect Workbench build" "$BUILD_USER"
    fi
    info "the build user is in no group but its own: not $WORK_GROUP (the check socket), not sudo, not the owner's"
    if build_user_entry >/dev/null; then
        for g in $(build_user_groups); do
            [ "$g" = "$BUILD_USER" ] || run gpasswd -d "$BUILD_USER" "$g"
        done
    fi
}

build_image() {
    # the image is made once and never formatted again; a host without the room or a mount that fails leaves the build
    # side for the next run with one warning, and nothing is made on the root filesystem in its place
    local avail dir
    dir=$(dirname -- "$BUILD_IMAGE")
    info "the build root $BUILD_ROOT: an ext4 image of $BUILD_SIZE_GB GB at $BUILD_IMAGE, allocated at once, made once and"
    info "mounted by an fstab line (nofail: a missing image never stops a boot); a runaway task fills the image only"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ fallocate -l %sG %s  # if missing, when %s keeps %s GB free after it\n' "$BUILD_SIZE_GB" \
            "$BUILD_IMAGE" "$dir" "$BUILD_FLOOR_GB"
        printf '+ chmod 600 %s  # if missing\n' "$BUILD_IMAGE"
        printf '+ mkfs.ext4 -q -F -L awb-build -m 0 %s  # if missing\n' "$BUILD_IMAGE"
    elif [ -e "$BUILD_IMAGE" ]; then
        skip "already there, never formatted again" mkfs.ext4 -q -F -L awb-build -m 0 "$BUILD_IMAGE"
    else
        avail=$(df --output=avail -B1G -- "$dir" 2>/dev/null | tail -n 1 | tr -d ' ') || avail=""
        if ! [[ "$avail" =~ ^[0-9]+$ ]] || [ "$avail" -lt $((BUILD_SIZE_GB + BUILD_FLOOR_GB)) ]; then
            info "warning: $dir has ${avail:-an unknown number of} GB free; the image needs $BUILD_SIZE_GB GB and leaves"
            info "$BUILD_FLOOR_GB GB: the build side waits for the next run, seal/verify.sh reports it"
            return 0
        fi
        show fallocate -l "${BUILD_SIZE_GB}G" "$BUILD_IMAGE"
        if ! fallocate -l "${BUILD_SIZE_GB}G" "$BUILD_IMAGE"; then
            rm -f -- "$BUILD_IMAGE"
            info "warning: the image could not be allocated, the build side waits for the next run"
            return 0
        fi
        run chmod 600 "$BUILD_IMAGE"
        show mkfs.ext4 -q -F -L awb-build -m 0 "$BUILD_IMAGE"
        if ! mkfs.ext4 -q -F -L awb-build -m 0 "$BUILD_IMAGE"; then
            rm -f -- "$BUILD_IMAGE"
            info "warning: the image could not be formatted and was removed, the build side waits for the next run"
            return 0
        fi
    fi
    run mkdir -p "$BUILD_ROOT"
    append_line "$FSTAB" "$BUILD_FSTAB"
    if [ "$dry_run" -eq 0 ] && mountpoint -q "$BUILD_ROOT"; then
        skip "already mounted" mount "$BUILD_ROOT"
    else
        try_run mount "$BUILD_ROOT"
    fi
    if [ "$dry_run" -eq 0 ] && ! mountpoint -q "$BUILD_ROOT"; then
        info "warning: $BUILD_ROOT is not mounted, so no build folder is made on the root filesystem in its place"
        return 0
    fi
    run chown root:root "$BUILD_ROOT"
    run chmod 755 "$BUILD_ROOT"
    build_mounted=1
}

as_owner_git() {
    # as_owner_git ARG...: git as the owner (git refuses a repository of another user)
    runuser -u "$owner" -- env HOME="$owner_home" git "$@"
}

find_build_source() {
    # the repository the release came from: the working tree in the full mode; in the update mode the folder sudo awb
    # deploy ran in, when it is a working tree of the owner that holds the release commit. Empty otherwise.
    local top
    build_source=""
    [ "$real" -eq 1 ] || return 0
    if [ "$update" -eq 0 ]; then
        build_source=$repo
        return 0
    fi
    top=$(as_owner_git -C "$PWD" rev-parse --show-toplevel 2>/dev/null) || return 0
    [ -n "$top" ] && [ "$(stat -c %U -- "$top" 2>/dev/null)" = "$owner" ] || return 0
    [[ "$release" =~ ^[0-9a-f]{40}$ ]] || return 0
    as_owner_git -C "$top" cat-file -e "$release^{commit}" 2>/dev/null || return 0
    build_source=$top
}

build_folders() {
    local main="$BUILD_ROOT/main.git"
    info "on the image: main.git (the owner's, bare, 755; main moves only here), wt/ (the build user's clones, 755),"
    info "runs/ (the runner's records, owner 700), home/ (the build user's own, 700)"
    run install -d -o "$owner" -g "$owner_gid" -m 755 "$main"
    run_unless_present "$main/HEAD" runuser -u "$owner" -- env HOME="$owner_home" git init --bare --quiet -b main "$main"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ %s  # once, while main.git has no main\n' "$(quote_args runuser -u "$owner" -- env HOME="$owner_home" \
            git -C "$main" fetch --no-tags --quiet "<the repository>" refs/heads/main:refs/heads/main)"
        info "<the repository> is the working tree of a first seal, in an update the folder sudo awb deploy ran in"
        info "when it holds the release commit"
    elif as_owner_git -C "$main" rev-parse --verify --quiet refs/heads/main >/dev/null 2>&1; then
        skip "main is there" git -C "$main" fetch --no-tags --quiet "<the repository>" refs/heads/main:refs/heads/main
    elif [ -n "$build_source" ]; then
        try_run runuser -u "$owner" -- env HOME="$owner_home" git -C "$main" fetch --no-tags --quiet "$build_source" \
            refs/heads/main:refs/heads/main
    else
        info "main.git has no main yet and this run found no repository to take it from: as the owner, run once"
        info "git -C $main fetch --no-tags <the working copy> refs/heads/main:refs/heads/main"
    fi
    run install -d -o "$BUILD_USER" -g "$BUILD_USER" -m 755 "$BUILD_ROOT/wt"
    run install -d -o "$owner" -g "$owner_gid" -m 700 "$BUILD_ROOT/runs"
    run install -d -o "$BUILD_USER" -g "$BUILD_USER" -m 700 "$BUILD_ROOT/home"
    build_board
}

build_venv() {
    info "the suite's venv in $BUILD_ROOT/venv, root owned: the build user runs it and never changes it"
    if ! (suite_venv "$BUILD_ROOT/venv"); then
        info "warning: the venv of the suite is not complete, seal/verify.sh reports it"
    fi
}

plain_value() {
    # plain_value TEXT: a value git config takes as it is (printable, no quote, backslash, comment sign or bracket)
    [ -n "$1" ] && [ "${#1}" -le 200 ] && [[ "$1" =~ ^[[:print:]]+$ ]] || return 1
    case "$1" in
        *'"'* | *'\'* | *';'* | *'#'* | *'['* | *']'*) return 1 ;;
    esac
}

build_identity() {
    # build_identity TARGET: the owner's name and mail (D-S16), from the repository the release came from (its own
    # config first, then his global one); the content is not printed. Without both, the file there stays as it is
    local target="$1" name="" mail="" tmp
    if [ "$real" -eq 0 ]; then
        printf '+ write %s (mode 644, root:root)  # [user] name and email of the owner, from git config in the repository\n' \
            "$target"
        return 0
    fi
    if [ -n "$build_source" ]; then
        name=$(as_owner_git -C "$build_source" config --get user.name 2>/dev/null) || name=""
        mail=$(as_owner_git -C "$build_source" config --get user.email 2>/dev/null) || mail=""
    else
        name=$(as_owner_git config --global --get user.name 2>/dev/null) || name=""
        mail=$(as_owner_git config --global --get user.email 2>/dev/null) || mail=""
    fi
    if ! plain_value "$name" || ! plain_value "$mail"; then
        info "warning: no plain user.name and user.email of the owner were found: $target stays as it is"
        return 0
    fi
    guard install "$target"
    printf '+ write %s (mode 644, root:root)  # [user] name and email of the owner\n' "$target"
    [ "$dry_run" -eq 0 ] || return 0
    tmp=$(mktemp "$(dirname -- "$target")/.awb-setup.XXXXXX")
    printf '# written by seal/setup.sh: a build task commits as the owner (D-S16); no safe.directory\n[user]\n\tname = %s\n\temail = %s\n' \
        "$name" "$mail" >"$tmp"
    chown root:root "$tmp"
    chmod 644 "$tmp"
    mv -f -- "$tmp" "$target"
}

build_template() {
    local t="$BUILD_ROOT/home-template" f
    info "the home template of a build task, root owned and immutable: .claude/settings.json (seal/build-claude),"
    info ".claude/CLAUDE.md (the repository's) and .gitconfig (the owner's identity, no safe.directory)"
    clear_target "$t" d
    clear_target "$t/.claude" d
    run install -d -o root -g root -m 755 "$t" "$t/.claude"
    clear_target "$t/.claude/settings.json" f
    if [ "$build_model" = key ]; then
        info "build_model = key: the settings read the model key in the task home with apiKeyHelper"
        write_file "$t/.claude/settings.json" 644 root:root < <(head -n 2 "$seal_dir/build-claude/settings.json"
            printf '%s\n' "$BUILD_KEY_HELPER"; tail -n +3 "$seal_dir/build-claude/settings.json")
    else
        run install -o root -g root -m 644 "$seal_dir/build-claude/settings.json" "$t/.claude/settings.json"
    fi
    clear_target "$t/.claude/CLAUDE.md" f
    run install -o root -g root -m 644 "$repo/CLAUDE.md" "$t/.claude/CLAUDE.md"
    clear_target "$t/.gitconfig" f
    build_identity "$t/.gitconfig"
    for f in "$t/.claude/settings.json" "$t/.claude/CLAUDE.md" "$t/.gitconfig" "$t/.claude" "$t"; do
        run_if_present "$f" chattr +i "$f"
    done
}

build_client() {
    local src="$owner_home/.local/share/claude/versions/$BUILD_CLIENT_VERSION"
    local bin="$BUILD_CLIENT/versions/$BUILD_CLIENT_VERSION" digest
    info "the client of the build user: $bin, a root owned copy of version $BUILD_CLIENT_VERSION from the owner's"
    info "native install, taken only with the pinned sha256; $BUILD_CLIENT/bin/claude runs it with the updater off"
    run install -d -o root -g root -m 755 "$BUILD_CLIENT" "$BUILD_CLIENT/versions" "$BUILD_CLIENT/bin"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ install -o root -g root -m 755 %s %s.partial  # if missing; kept only with sha256 %s\n' "$src" \
            "$bin" "$BUILD_CLIENT_SHA256"
        printf '+ mv -f -- %s.partial %s  # if missing\n' "$bin" "$bin"
    elif [ -f "$bin" ] && [ "$(sha256sum <"$bin" | cut -d' ' -f1)" = "$BUILD_CLIENT_SHA256" ]; then
        skip "there, with the pinned digest" install -o root -g root -m 755 "$src" "$bin"
    elif [ -L "$src" ] || [ ! -f "$src" ]; then
        info "warning: version $BUILD_CLIENT_VERSION is not in the owner's native install: the build client waits,"
        info "seal/verify.sh reports it"
    else
        run install -o root -g root -m 755 "$src" "$bin.partial"
        digest=$(sha256sum <"$bin.partial" | cut -d' ' -f1)
        if [ "$digest" = "$BUILD_CLIENT_SHA256" ]; then
            run mv -f -- "$bin.partial" "$bin"
        else
            run rm -f -- "$bin.partial"
            info "warning: the copy of $BUILD_CLIENT_VERSION does not carry the pinned digest and was removed"
        fi
    fi
    write_file "$BUILD_CLIENT/bin/claude" 755 root:root <<WRAP
#!/bin/sh
# written by seal/setup.sh: the pinned client of the build user (presentations/self-build); the updater and the auto
# memory stay off, whatever a settings file says
DISABLE_AUTOUPDATER=1
CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
export DISABLE_AUTOUPDATER CLAUDE_CODE_DISABLE_AUTO_MEMORY
exec $bin "\$@"
WRAP
    build_model_credential
}

build_model_credential() {
    # D-S7 changed: build_model = login (the default) runs the tasks on his subscription login, which he makes once
    # in the build user's own home; no key copy then, and one left from an earlier key mode goes. key: the copy below
    if [ "$build_model" = key ]; then
        info "the build model is the model key (build_model = key in the host file)"
        build_model_key
        return 0
    fi
    info "the build model is his login (build_model = login): the build user's home keeps the client's login, made"
    info "once by him as the owner: sudo -u $BUILD_USER -H $BUILD_CLIENT/bin/claude login"
    info "no model key is copied; a copy left from the key mode is removed"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ rm -f -- %s  # if present\n' "$BUILD_MODEL_KEY"
    elif [ -e "$BUILD_MODEL_KEY" ] || [ -L "$BUILD_MODEL_KEY" ]; then
        run rm -f -- "$BUILD_MODEL_KEY"
    else
        skip "not present" rm -f -- "$BUILD_MODEL_KEY"
    fi
}

build_model_key() {
    # the model key of a build task (D-S7): a copy of the source the host file names, the owner's alone (400, in the
    # root folder /etc/awb), refreshed when the source changes. The runner hands it to each task's home and removes
    # it with the home. Its content is never printed. A dedicated key later is one line: build_model_key_source.
    local src dst="$BUILD_MODEL_KEY"
    src=$(kept_conf build_model_key_source "$BUILD_MODEL_KEY_SOURCE")
    info "the model key of a build task: $dst, a copy of $src for the owner alone (400), refreshed when the source"
    info "changes; the runner copies it into each task's home and removes it with the home"
    if [ "$dry_run" -eq 1 ]; then
        printf '+ install -o %s -g %s -m 400 %s %s.partial  # when missing or the source changed\n' "$owner" \
            "$owner_gid" "$src" "$dst"
        printf '+ mv -f -- %s.partial %s  # when missing or the source changed\n' "$dst" "$dst"
        return 0
    fi
    if [ -L "$src" ] || [ ! -f "$src" ]; then
        info "warning: the source of the model key is not a regular file: the build tasks wait, seal/verify.sh reports it"
        return 0
    fi
    if [ -f "$dst" ] && [ ! -L "$dst" ] && cmp -s -- "$src" "$dst" &&
        [ "$(stat -c '%U %a' -- "$dst")" = "$owner 400" ]; then
        skip "the copy is current" install -o "$owner" -g "$owner_gid" -m 400 "$src" "$dst"
        return 0
    fi
    run install -o "$owner" -g "$owner_gid" -m 400 "$src" "$dst.partial"
    run mv -f -- "$dst.partial" "$dst"
}

build_board() {
    info "the build line of the board: $work_home/tcp-shared/build, the work user's with the group $WORK_GROUP (3770),"
    info "where the runner writes state.tsv for awb board and the portal"
    run install -d -o "$WORK_USER" -g "$WORK_GROUP" -m 3770 "$work_home/tcp-shared/build"
}

build_failed_unit() {
    # written by step_units (not in --units-only), not a seal/*.service file: a template unit has no state a deploy
    # could read or restart
    info "$BUILD_FAILED_UNIT: the oneshot every task unit of the build runner names in OnFailure=; as the owner it"
    info "writes one board line when a task unit fails while no runner waits on it (awb build failed)"
    write_file "$UNITS_DIR/$BUILD_FAILED_UNIT" 644 root:root <<EOF
# Written by seal/setup.sh (presentations/self-build SB5). Every awb-task-* unit of the build runner names
# awb-task-failed@<unit>.service in OnFailure=. While a runner holds its lock it waits on the unit and reports it;
# otherwise this writes one row into the board's state.tsv; the board line names the unit, its result and the
# time until the runner writes the file again.
[Unit]
Description=Architect Workbench build: the task unit %i failed
RequiresMountsFor=$BUILD_ROOT

[Service]
Type=oneshot
User=$owner
Group=$WORK_GROUP
UMask=0027
NoNewPrivileges=yes
ExecStart=$BIN_LINK build failed %i
TimeoutStartSec=60
LimitCORE=0
EOF
}

step_build() {
    info "the build side of the self-build (presentations/self-build, SB1, SB3 and SB5)"
    step_build_user
    build_client
    info "the commit hooks of a build clone come with the release: $OPT/src/hooks-build (the installed gate"
    info "--no-register by the installed interpreter's absolute path); the runner points each clone's hooks there"
    build_image
    if [ "$dry_run" -eq 0 ] && [ "$build_mounted" -eq 0 ]; then
        return 0
    fi
    find_build_source
    build_folders
    build_venv
    build_template
}

check_mirror() {
    # check_mirror DIR: an absolute folder that neither lies in nor holds the vault, the password store or keys
    local src="${1%/}" q
    case "$src" in
        /?*) ;;
        *) die "a mirror folder must be an absolute path" ;;
    esac
    [[ "$src" =~ ^[A-Za-z0-9_./-]+$ ]] || die "a mirror folder may hold letters, digits, dots, hyphens, underscores and slashes only"
    for q in "${protected[@]}" "$owner_home/.ssh" "$owner_home/.claude" "$owner_home/.gnupg"; do
        if inside "$src" "$q" || inside "$q" "$src"; then
            die "refused: a mirror would show the owner's vault, password store or keys"
        fi
    done
}

step_mirrors() {
    local src target
    [ "$mirrors" -eq 1 ] || return 0
    info "read-only bind mounts of the public doc mirrors under $MIRROR_ROOT"
    run mkdir -p "$MIRROR_ROOT"
    run chmod 755 "$MIRROR_ROOT"
    for src in "${mirror_sources[@]}"; do
        src="${src%/}"
        if [ "$dry_run" -eq 0 ] && [ ! -d "$src" ]; then
            die "a mirror folder does not exist"
        fi
        target="$MIRROR_ROOT/$(basename -- "$src")"
        run mkdir -p "$target"
        append_line /etc/fstab "$src $target none bind,ro 0 0"
        if [ "$dry_run" -eq 0 ] && mountpoint -q "$target"; then
            skip "already mounted" mount "$target"
        else
            run mount "$target"
        fi
    done
}

steps() {
    if [ "$test_root" -eq 1 ]; then
        step_code
        return 0
    fi
    if [ "$update" -eq 1 ]; then
        update_steps
        return 0
    fi
    step_users
    step_homes
    step_moves
    step_code
    step_conf
    step_units
    step_egress
    step_needrestart
    step_holds
    step_kb_hooks
    step_ssh
    step_client
    step_managed
    step_build
    step_mirrors
}

update_steps() {
    # the steps of a deploy, named one by one: never "every step minus some"
    if [ "$units_only" -eq 1 ]; then
        step_units
        return 0
    fi
    step_code
    step_conf
    step_units
    step_egress
    step_needrestart
    step_holds
    step_kb_hooks
    step_client
    step_managed
    step_build
    step_mirrors
}

# --------------------------------------------------------------------------- run

for src in "${mirror_sources[@]}"; do
    check_mirror "$src"
done
build_model=$(build_model_mode)

if [ "$dry_run" -eq 1 ]; then
    if [ "$update" -eq 1 ]; then
        info "dry run of the update for the owner $owner: every command is printed, none is run"
    else
        info "dry run for the owner $owner: every command is printed, none is run"
    fi
    steps
    info "dry run done, nothing was changed"
    exit 0
fi

# the whole plan runs once without changing anything: a step that the guard refuses stops the setup before the
# first change
if ! (dry_run=1 && steps) >/dev/null; then
    die "the plan was refused, nothing was changed"
fi
if [ "$update" -eq 1 ]; then
    info "update for the owner $owner"
else
    info "seal for the owner $owner"
fi
steps
if [ "$update" -eq 0 ] && [ "$test_root" -eq 0 ]; then
    info "done. Check the seal with: sudo $seal_dir/verify.sh"
    info "next: sudo awb deploy"
fi
