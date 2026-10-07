#!/usr/bin/env bash
# Verify the seal of the Architect Workbench (INTERFACES.md, section "The seal"). Run by the owner with sudo after
# seal/setup.sh:
#
#   sudo seal/verify.sh [--dry-run]
#
# As the work user each of these must fail: read the register (plain or .gpg), list the vault, list the owner's
# password store, .claude, .ssh, home or any folder in the owner's home, sudo -n true. The work user is in none of the
# groups of the owner or of the host admins. The check socket answers ping, the work user can write tcp-shared and
# the owner can write the outbox. The client files of the work user are those of the repository and immutable, the
# managed drop-in carries the hooks. One line per check, PASS or FAIL; exit 1 on any FAIL.
# --dry-run lists the checks and runs none (it works without root). Paths come from /etc/awb/paths.conf when it
# is there, home folders from getent passwd. No line names a folder of the owner: they are counted.
set -euo pipefail

WORK_USER=awb
WORK_GROUP=awb
OPT=/opt/tcp-awb
CONF_FILE=/etc/awb/paths.conf
MANAGED_FILE=/etc/claude-code/managed-settings.d/awb-workbench.json
CLIENT_FILES="settings.json CLAUDE.md skills/drafting/SKILL.md"
seal_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
FORBIDDEN_GROUPS=(ubuntu docker adm sudo lxd)

usage() {
    echo "usage: sudo $0 [--dry-run]" >&2
    exit 2
}

die() {
    echo "verify: $*" >&2
    exit 2
}

dry_run=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) dry_run=1 ;;
        -h | --help)
            echo "usage: sudo $0 [--dry-run]"
            exit 0
            ;;
        *) usage ;;
    esac
    shift
done

owner="${SUDO_USER:-}"
owner_home="<owner-home>"
owner_group=""
work_home="<work-home>"
if [ "$dry_run" -eq 0 ]; then
    [ -n "$owner" ] || die "run this with sudo as the owner (SUDO_USER is empty)"
    [ "$owner" != root ] || die "the owner must be a user, not root"
    [ "$(id -u)" -eq 0 ] || die "run this with sudo or use --dry-run"
    getent passwd "$WORK_USER" >/dev/null || die "the work user does not exist, run seal/setup.sh first"
fi
if [ -n "$owner" ] && owner_entry=$(getent passwd "$owner"); then
    owner_home=$(printf '%s\n' "$owner_entry" | cut -d: -f6)
    owner_group=$(getent group "$(printf '%s\n' "$owner_entry" | cut -d: -f4)" | cut -d: -f1) || owner_group=""
elif [ "$dry_run" -eq 0 ]; then
    die "the owner is not a user of this host"
fi
[ -n "$owner" ] || owner="<owner>"
if work_entry=$(getent passwd "$WORK_USER"); then
    work_home=$(printf '%s\n' "$work_entry" | cut -d: -f6)
fi

conf_value() {
    # conf_value KEY: the value of KEY in the host file, empty when there is none
    [ -r "$CONF_FILE" ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONF_FILE" | tail -n 1
}

vault=$(conf_value vault)
vault="${vault:-$owner_home/tcp-vault}"
shared=$(conf_value shared)
shared="${shared:-$work_home/tcp-shared}"
socket=$(conf_value check_socket)
socket="${socket:-/run/awb/check.sock}"

forbidden=("${FORBIDDEN_GROUPS[@]}")
if [ -n "$owner_group" ] && [[ " ${forbidden[*]} " != *" $owner_group "* ]]; then
    forbidden+=("$owner_group")
fi

PING_PY=$(
    cat <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
s.connect(sys.argv[1])
s.sendall(b'{"op":"ping"}\n')
buf = b""
while not buf.endswith(b"\n"):
    part = s.recv(4096)
    if not part:
        break
    buf += part
sys.exit(0 if json.loads(buf.decode("utf-8")).get("ok") is True else 1)
PY
)
WRITE_SH='f=$(mktemp -p "$1" .verify.XXXXXX) && rm -f -- "$f"'

fails=0
checks=0

report() {
    # report STATUS WHAT: STATUS 0 is PASS
    checks=$((checks + 1))
    if [ "$1" -eq 0 ]; then
        printf 'PASS  %s\n' "$2"
    else
        printf 'FAIL  %s\n' "$2"
        fails=$((fails + 1))
    fi
}

as_work() { runuser -u "$WORK_USER" -- "$@"; }

denied() {
    # denied WHAT CMD...: PASS when CMD fails
    local what="$1"
    shift
    if [ "$dry_run" -eq 1 ]; then
        printf 'CHECK %s\n' "$what"
        return 0
    fi
    if "$@" >/dev/null 2>&1; then report 1 "$what"; else report 0 "$what"; fi
}

allowed() {
    # allowed WHAT CMD...: PASS when CMD succeeds
    local what="$1"
    shift
    if [ "$dry_run" -eq 1 ]; then
        printf 'CHECK %s\n' "$what"
        return 0
    fi
    if "$@" >/dev/null 2>&1; then report 0 "$what"; else report 1 "$what"; fi
}

owner_folders() {
    local n=0 d
    if [ "$dry_run" -eq 1 ]; then
        printf 'CHECK the work user cannot list any folder in the owner'"'"'s home (each one, counted)\n'
        return 0
    fi
    shopt -s nullglob
    for d in "$owner_home"/*/; do
        [ -d "$d" ] || continue
        n=$((n + 1))
        denied "the work user cannot list folder $n in the owner's home" as_work ls -A -- "$d"
    done
    shopt -u nullglob
    [ "$n" -gt 0 ] || report 0 "the owner's home holds no folder"
}

groups_of_work_user() {
    local g names=""
    if [ "$dry_run" -eq 0 ]; then
        names=" $(id -nG "$WORK_USER") "
    fi
    for g in "${forbidden[@]}"; do
        if [ "$dry_run" -eq 1 ]; then
            printf 'CHECK the work user is not in the group %s\n' "$g"
        elif [[ "$names" == *" $g "* ]]; then
            report 1 "the work user is not in the group $g"
        else
            report 0 "the work user is not in the group $g"
        fi
    done
}

[ "$dry_run" -eq 0 ] || printf '# dry run: the checks for the owner %s, none is run\n' "$owner"

denied "the work user cannot read the register" as_work cat -- "$vault/register.tsv"
denied "the work user cannot read the encrypted register" as_work cat -- "$vault/register.tsv.gpg"
denied "the work user cannot list the vault" as_work ls -A -- "$vault"
denied "the work user cannot list the owner's password store" as_work ls -A -- "$owner_home/.password-store"
denied "the work user cannot list the owner's .claude" as_work ls -A -- "$owner_home/.claude"
denied "the work user cannot list the owner's .ssh" as_work ls -A -- "$owner_home/.ssh"
denied "the work user cannot list the owner's home" as_work ls -A -- "$owner_home"
denied "the work user cannot enter the owner's home" as_work test -x "$owner_home"
owner_folders
denied "the work user cannot run sudo -n true" as_work sudo -n true
groups_of_work_user
denied "the work user cannot write the code in $OPT" as_work test -w "$OPT/src"
allowed "the check socket answers ping" as_work python3 -c "$PING_PY" "$socket"
allowed "the work user can write tcp-shared" as_work sh -c "$WRITE_SH" verify "$shared"
allowed "the owner can write the outbox" runuser -u "$owner" -- sh -c "$WRITE_SH" verify "$shared/outbox"
allowed "the outbox carries the setgid and the sticky bit" test "$(stat -c %a -- "$shared/outbox")" = "3770"
allowed "the owner can write the tenant history" runuser -u "$owner" -- sh -c "$WRITE_SH" verify "$shared/tenants"
immutable() {
    # immutable FILE: the file carries the immutable flag (lsattr prints the flags first, i among them)
    local flags
    flags=$(lsattr -d -- "$1" 2>/dev/null | cut -d' ' -f1) || return 1
    [[ "$flags" == *i* ]]
}

for f in $CLIENT_FILES; do
    allowed "the client file $f of the work user is the one of the repository" \
        cmp -s -- "$seal_dir/work-claude/$f" "$work_home/.claude/$f"
    allowed "the client file $f of the work user is immutable" immutable "$work_home/.claude/$f"
done
for d in skills skills/drafting; do
    allowed "the skill folder $d of the work user is immutable" immutable "$work_home/.claude/$d"
done
allowed "the git identity of the work user is the one of the repository" \
    cmp -s -- "$seal_dir/work-gitconfig" "$work_home/.gitconfig"
allowed "the git identity of the work user is immutable" immutable "$work_home/.gitconfig"
allowed "the managed client settings carry the Workbench hooks" \
    cmp -s -- "$seal_dir/work-claude/managed-settings.json" "$MANAGED_FILE"
allowed "the host file names the work user, so the hooks leave other users alone" \
    sh -c 'grep -Eq "^[[:space:]]*work_user[[:space:]]*=[[:space:]]*$2[[:space:]]*$" "$1"' verify "$CONF_FILE" \
    "$WORK_USER"
allowed "the owner is in the group $WORK_GROUP" sh -c 'id -nG "$1" | tr " " "\n" | grep -qx "$2"' verify \
    "$owner" "$WORK_GROUP"

if [ "$dry_run" -eq 1 ]; then
    exit 0
fi
printf '# %d checks, %d failed\n' "$checks" "$fails"
[ "$fails" -eq 0 ] || exit 1
