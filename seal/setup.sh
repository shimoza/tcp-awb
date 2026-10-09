#!/usr/bin/env bash
# Seal the Architect Workbench on this host (INTERFACES.md, section "The seal"). Run by the owner with sudo:
#
#   sudo seal/setup.sh [--dry-run] [--mirrors DIR...]                     the first seal of a host, every step
#   releases/<commit>/seal/setup.sh --update [--units-only] [--dry-run] [--mirrors DIR...]
#
# --update is what `sudo awb deploy` runs, from the release folder it has just extracted, never from a working
# tree. Its steps are an explicit allow list (update_steps): the code, the host file, the units, the needrestart
# rule, the package holds of cloudflared and terraform, the client files and the managed drop-in. The users, the homes, the moves, the ssh keys and (without
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

usage() {
    echo "usage: sudo $0 [--dry-run] [--update [--units-only]] [--mirrors DIR...]" >&2
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
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            dry_run=1
            real=0
            ;;
        --update) update=1 ;;
        --units-only) units_only=1 ;;
        --mirrors) mirrors=1 ;;
        -h | --help)
            echo "usage: sudo $0 [--dry-run] [--update [--units-only]] [--mirrors DIR...]"
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

step_conf() {
    local web_keys owner_host
    info "the host file of the Workbench paths"
    run mkdir -p "$CONF_DIR"
    run chmod 755 "$CONF_DIR"
    owner_host=$(kept_conf owner_host "")
    web_keys="front_socket = $(kept_conf front_socket /run/awb-web/front.sock)
cloudflared_user = $(kept_conf cloudflared_user cloudflared)"
    [ -z "$owner_host" ] || web_keys="$web_keys
owner_host = $owner_host"
    write_file "$CONF_FILE" 644 root:root <<EOF
# written by seal/setup.sh, read by awb/config.py
owner = $owner
work_user = $WORK_USER
shared = $work_home/tcp-shared
vault = $vault
projects = $work_home
kb = $work_home/tcp-kb
check_socket = $CHECK_SOCKET
mirrors = $MIRROR_ROOT
$web_keys
EOF
}

step_units() {
    local f name
    info "the units of seal/*.service, rendered with the owner: the vault daemon and the key service run as the owner"
    info "with the group $WORK_GROUP, the Ask page as awb-ask (its key file stays the owner's step), the portal as"
    info "the work user. A running unit is left running: sudo awb deploy restarts what changed"
    info "The vault locks on a stop, a crash or a reboot. awb vault reload (what awb deploy uses after a code update)"
    info "hands the passphrase to the new process over a private socket pair and keeps it unlocked"
    for f in "$seal_dir"/*.service; do
        name=$(basename -- "$f")
        write_file "$UNITS_DIR/$name" 644 root:root < <(sed -e "s|@OWNER@|$owner|g" "$f")
    done
    run systemctl daemon-reload
    [ "$units_only" -eq 0 ] || return 0
    for f in "$seal_dir"/*.service; do
        try_run systemctl enable --now "$(basename -- "$f")"
    done
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
    step_needrestart
    step_holds
    step_kb_hooks
    step_ssh
    step_client
    step_managed
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
    step_needrestart
    step_holds
    step_kb_hooks
    step_client
    step_managed
    step_mirrors
}

# --------------------------------------------------------------------------- run

for src in "${mirror_sources[@]}"; do
    check_mirror "$src"
done

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
