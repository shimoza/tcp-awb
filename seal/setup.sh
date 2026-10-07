#!/usr/bin/env bash
# Seal the Architect Workbench on this host (INTERFACES.md, section "The seal"). Run by the owner with sudo:
#
#   sudo seal/setup.sh [--dry-run] [--mirrors DIR...]
#
# Two users. The owner (the user who runs sudo) keeps the vault. The work user awb has no sudo, is in none of the
# groups of the owner or of the host admins. It runs every working session. Its home holds tcp-shared, tcp-kb and
# the projects. The code is installed read-only to /opt/tcp-awb, the vault daemon runs as the owner. The command
# /usr/local/bin/awb is a root owned wrapper that runs the installed interpreter isolated (python3 -I): no package
# of the invoking user's site folder, PYTHONPATH or working folder is loaded ahead of the installed code.
#
# The client files of the work user are root owned and immutable; the five hooks are also installed as managed
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
UNIT_NAME=awb-vaultd.service
UNIT_FILE=/etc/systemd/system/awb-vaultd.service
NEEDRESTART_FILE=/etc/needrestart/conf.d/awb.conf
CHECK_SOCKET=/run/awb/check.sock
MIRROR_ROOT=/srv/tcp-mirrors
MANAGED_DIR=/etc/claude-code/managed-settings.d
MANAGED_FILE=/etc/claude-code/managed-settings.d/awb-workbench.json
CLIENT_FILES="settings.json CLAUDE.md skills/drafting/SKILL.md"
FORBIDDEN_GROUPS=(ubuntu docker adm sudo lxd)

usage() {
    echo "usage: sudo $0 [--dry-run] [--mirrors DIR...]" >&2
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
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            dry_run=1
            real=0
            ;;
        --mirrors) mirrors=1 ;;
        -h | --help)
            echo "usage: sudo $0 [--dry-run] [--mirrors DIR...]"
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

# --------------------------------------------------------------------------- who is who

owner="${SUDO_USER:-}"
[ -n "$owner" ] || die "run this with sudo as the owner (SUDO_USER is empty)"
[ "$owner" != root ] || die "the owner must be a user, not root"
[ "$owner" != "$WORK_USER" ] || die "the owner must not be the work user"
if [ "$dry_run" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
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
    chown "$og" "$tmp"
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

archive_code() {
    # git archive runs as the owner (git refuses a repository of another user), tar unpacks as root
    guard git "$repo" "$OPT/src"
    printf '+ %s | %s\n' "$(quote_args runuser -u "$owner" -- env HOME="$owner_home" git -C "$repo" archive HEAD)" \
        "$(quote_args tar -x -C "$OPT/src")"
    [ "$dry_run" -eq 1 ] || runuser -u "$owner" -- env HOME="$owner_home" git -C "$repo" archive HEAD |
        tar -x -C "$OPT/src"
}

step_code() {
    info "the code, read-only in $OPT with its own virtual environment"
    run mkdir -p "$OPT"
    run rm -rf -- "$OPT/src"
    run mkdir -p "$OPT/src"
    archive_code
    run_unless_present "$OPT/venv/bin/python" python3 -m venv --system-site-packages "$OPT/venv"
    # editable, so that awb finds rules/ and CLAUDE.md next to its package in $OPT/src
    run "$OPT/venv/bin/pip" install --quiet --no-deps --no-build-isolation --editable "$OPT/src"
    run chown -R root:root "$OPT"
    run chmod -R go-w "$OPT"
    # a wrapper, not a symlink to the venv script: -I ignores the invoking user's site folder, PYTHONPATH and the
    # working folder, which would otherwise load ahead of the installed code (T3); the AWB_* variables survive
    write_file "$BIN_LINK" 755 root:root <<EOF
#!/bin/sh
# written by seal/setup.sh: the installed Workbench, run isolated from the invoking user's packages
exec $OPT/venv/bin/python3 -I -m awb "\$@"
EOF
}

step_conf() {
    info "the host file of the Workbench paths"
    run mkdir -p "$CONF_DIR"
    run chmod 755 "$CONF_DIR"
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
EOF
}

step_daemon() {
    local unit
    info "the vault daemon runs as the owner with the group $WORK_GROUP"
    unit=$(sed -e "s|@OWNER@|$owner|g" "$seal_dir/$UNIT_NAME")
    write_file "$UNIT_FILE" 644 root:root <<<"$unit"
    run systemctl daemon-reload
    run systemctl enable --now "$UNIT_NAME"
    info "after an update of the code run: systemctl restart $UNIT_NAME (the vault locks, unlock it again)"
    info "needrestart leaves the vault daemon and the key service running after a package upgrade"
    run mkdir -p "$(dirname "$NEEDRESTART_FILE")"
    write_file "$NEEDRESTART_FILE" 644 root:root <"$seal_dir/needrestart-awb.conf"
}

step_ssh() {
    local keys="$owner_home/.ssh/authorized_keys"
    info "the owner's ssh keys open the work user too"
    run mkdir -p "$work_home/.ssh"
    run chown "$WORK_USER:$WORK_GROUP" "$work_home/.ssh"
    run chmod 700 "$work_home/.ssh"
    run_if_present "$keys" install -o "$WORK_USER" -g "$WORK_GROUP" -m 600 "$keys" "$work_home/.ssh/authorized_keys"
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
        run_if_present "$work_home/.claude/$d" chattr -i "$work_home/.claude/$d"
    done
    run install -d -o root -g "$WORK_GROUP" -m 755 "$work_home/.claude/skills" "$work_home/.claude/skills/drafting"
    for f in $CLIENT_FILES; do
        target="$work_home/.claude/$f"
        run_if_present "$target" chattr -i "$target"
        run install -o root -g "$WORK_GROUP" -m 644 "$seal_dir/work-claude/$f" "$target"
        try_run chattr +i "$target"
    done
    for d in skills/drafting skills; do
        try_run chattr +i "$work_home/.claude/$d"
    done
}

step_managed() {
    local src="$seal_dir/work-claude/managed-settings.json"
    info "the five hooks as managed settings of the client (a drop-in file): no user or project file can switch"
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
    step_users
    step_homes
    step_moves
    step_code
    step_conf
    step_daemon
    step_ssh
    step_client
    step_managed
    step_mirrors
}

# --------------------------------------------------------------------------- run

for src in "${mirror_sources[@]}"; do
    check_mirror "$src"
done

if [ "$dry_run" -eq 1 ]; then
    info "dry run for the owner $owner: every command is printed, none is run"
    steps
    info "dry run done, nothing was changed"
    exit 0
fi

# the whole plan runs once without changing anything: a step that the guard refuses stops the setup before the
# first change
if ! (dry_run=1 && steps) >/dev/null; then
    die "the plan was refused, nothing was changed"
fi
info "seal for the owner $owner"
steps
info "done. Check the seal with: sudo $seal_dir/verify.sh"
