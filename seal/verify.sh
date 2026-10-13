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
# managed drop-in carries the hooks. The egress table of the work user is loaded in the mode of the host file, takes
# the work user's uid alone, counts a planted connection of the work user to 192.0.2.1 (and refuses it in the mode
# enforce) and leaves one of the owner's uid, which the key service runs under, uncounted; its timer is active.
# The build side of the self-build (presentations/self-build, SB1): the build user, its image and folders with their
# owners and modes, the home template, the pinned client, the build hooks and the runner's unit; then N1 to N5 as the
# build user (the vault, the host file, the blocklist, four sockets, sudo, two homes, a push into main.git each fail)
# and, inside a probe unit with the properties of a task, a write under /opt and runs/, a listing of runs/ and of the
# work user's home, the host file and the check socket fail. The network of a task is probed by the runner's own
# code (`awb build probe`, as the owner, SB5): a unit of the runner's systemd-run form with the address list of a
# task's session reaches the model endpoint (and with build_model = login the token host of his login) while a TCP
# API host and GitHub stay closed. A probe counts only after the unit was shown to run as the build user. The
# oneshot awb-task-failed@.service that every task unit names in OnFailure= is installed.
# One line per check, PASS or FAIL; exit 1 on any FAIL.
# --dry-run lists the checks and runs none (it works without root). Paths come from /etc/awb/paths.conf when it
# is there, home folders from getent passwd. No line names a folder of the owner: they are counted.
set -euo pipefail

WORK_USER=awb
WORK_GROUP=awb
OPT=/opt/tcp-awb
CONF_FILE=/etc/awb/paths.conf
BUILD_MODEL_KEY=/etc/awb/build-model.key     # the owner's copy for the runner (build_model = key), as in setup.sh
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

# AWB_VERIFY_CONF=FILE, with --dry-run only and never as root (the tests set it): the host file the listed checks
# take their paths from, in place of /etc/awb/paths.conf, so that a dry run never reads the host file of the host
# it runs on; a FILE that is not there stands for a fresh host
conf_read=$CONF_FILE
if [ -n "${AWB_VERIFY_CONF:-}" ]; then
    [ "$dry_run" -eq 1 ] || die "AWB_VERIFY_CONF goes with --dry-run only"
    [ "$(id -u)" -ne 0 ] || die "AWB_VERIFY_CONF is for the tests and refused as root"
    conf_read=$AWB_VERIFY_CONF
fi

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
    local from="${conf_read:-$CONF_FILE}"
    [ -r "$from" ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$from" | tail -n 1
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
kb=$(conf_value kb)
kb="${kb:-$work_home/tcp-kb}"
for h in pre-commit commit-msg pre-push; do
    allowed "the knowledge base carries the $h hook of the commit gate" \
        grep -qF -- "# awb commit gate, written by awb gate --install" "$kb/.git/hooks/$h"
done
allowed "the owner is in the group $WORK_GROUP" sh -c 'id -nG "$1" | tr " " "\n" | grep -qx "$2"' verify \
    "$owner" "$WORK_GROUP"

# --------------------------------------------------------------------------- the egress filter (seal/egress)

EGRESS_TABLE=awb_egress
PROBE_ADDR=192.0.2.1
# exit 0 connected, 3 refused (a reset), 4 no answer within 2 seconds or another error
PROBE_PY='import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect((sys.argv[1], 443))
except ConnectionRefusedError:
    sys.exit(3)
except OSError:
    sys.exit(4)
sys.exit(0)'

probe_packets() {
    # the packets of the named counter c_probe, empty when the table is not loaded
    nft list counter inet "$EGRESS_TABLE" c_probe 2>/dev/null | sed -n 's/.*packets \([0-9][0-9]*\).*/\1/p' | head -n 1
}

egress_checks() {
    local mode refused out rules work_uid pid key_uid before after rc
    if [ "$dry_run" -eq 1 ]; then
        printf 'CHECK %s\n' "the egress table of the work user is loaded" \
            "the egress filter runs in the mode of the host file (count or enforce)" \
            "only the work user's uid enters the egress chain (the key service's uid passes untouched)" \
            "the key service runs under a uid other than the work user's" \
            "a planted connection of the work user outside the list is counted (and refused in the mode enforce)" \
            "a planted connection of the owner's uid passes the filter uncounted" \
            "the timer of the egress filter is active"
        return 0
    fi
    mode=$(conf_value egress)
    mode="${mode:-count}"
    allowed "the egress table of the work user is loaded" nft list table inet "$EGRESS_TABLE"
    refused=$(nft list chain inet "$EGRESS_TABLE" refused 2>/dev/null || true)
    case "$mode" in
        count) [[ "$refused" == *accept* && "$refused" != *reject* ]] && out=0 || out=1 ;;
        enforce) [[ "$refused" == *"reject with tcp reset"* ]] && out=0 || out=1 ;;
        *) out=1 ;;
    esac
    report "$out" "the egress filter runs in the mode of the host file ($mode)"
    work_uid=$(id -u "$WORK_USER")
    rules=$(nft list chain inet "$EGRESS_TABLE" output 2>/dev/null | grep -Ev '^[[:space:]]*(table|chain|type|\}|$)' || true)
    if [ "$(printf '%s\n' "$rules" | grep -c .)" -eq 1 ] &&
        printf '%s\n' "$rules" | grep -Eq "^[[:space:]]*meta skuid (\"?$WORK_USER\"?|$work_uid) jump work$"; then
        out=0
    else
        out=1
    fi
    report "$out" "only the work user's uid enters the egress chain (the key service's uid passes untouched)"
    pid=$(systemctl show -p MainPID --value awb-keyd.service 2>/dev/null || echo 0)
    key_uid=""
    [ "${pid:-0}" = 0 ] || key_uid=$(stat -c %u "/proc/$pid" 2>/dev/null || true)
    [ -n "$key_uid" ] && [ "$key_uid" != "$work_uid" ] && out=0 || out=1
    report "$out" "the key service runs under a uid other than the work user's"
    before=$(probe_packets)
    rc=0
    as_work python3 -I -c "$PROBE_PY" "$PROBE_ADDR" >/dev/null 2>&1 || rc=$?
    after=$(probe_packets)
    out=1
    if [ -n "$before" ] && [ -n "$after" ] && [ "$after" -gt "$before" ]; then
        if [ "$mode" != enforce ] || [ "$rc" -eq 3 ]; then
            out=0
        fi
    fi
    report "$out" "a planted connection of the work user outside the list is counted (and refused in the mode enforce)"
    before=$(probe_packets)
    runuser -u "$owner" -- python3 -I -c "$PROBE_PY" "$PROBE_ADDR" >/dev/null 2>&1 || true
    after=$(probe_packets)
    [ -n "$before" ] && [ "$before" = "$after" ] && out=0 || out=1
    report "$out" "a planted connection of the owner's uid passes the filter uncounted"
    allowed "the timer of the egress filter is active" systemctl is-active --quiet awb-egress.timer
}

egress_checks

# --------------------------------------------------------------------------- the build side (presentations/self-build)

BUILD_USER=awb-build
BUILD_IMAGE=/var/lib/awb-build.img
BUILD_SIZE_GB=20
BUILD_CLIENT=/opt/claude-build
MODEL_HOST=api.anthropic.com
TOKEN_HOST=platform.claude.com        # awb/buildrun.py TOKEN_HOST: the refresh of his login
TCP_API_HOST=iam.eu-de.otc.t-systems.com
GITHUB_HOST=github.com
BUILD_FAILED_UNIT=awb-task-failed@.service
build_root=$(conf_value build_root)
build_root="${build_root:-/srv/awb-build}"
# exit 0 when the unix socket at $1 accepts a connection
CONNECT_PY='import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(3)
try:
    s.connect(sys.argv[1])
except OSError:
    sys.exit(1)
sys.exit(0)'
# a commit pushed into the bare repository $1 from a fresh repository in /tmp
PUSH_SH='d=$(mktemp -d) && git -C "$d" init -q && git -C "$d" -c user.name=probe -c user.email=probe@localhost commit -q \
--allow-empty -m probe && git -C "$d" push -q "$1" HEAD:refs/heads/verify-probe; rc=$?; rm -rf -- "$d"; exit $rc'

as_build() { runuser -u "$BUILD_USER" -- env HOME="$build_root/home" "$@"; }

probe_allow=""
probe_names=""
probe_ran=0

probe() {
    # probe CMD...: CMD as the build user in a transient unit with the properties of a build task (DESIGN.md section 4):
    # the system read only, no home, a private /tmp, no new privileges, /etc/awb inaccessible, no network but the
    # model endpoint's addresses resolved now (no resolver: the stub of /etc/resolv.conf is the host itself), a hosts
    # file of loopback over /etc/hosts and a resolv.conf without a nameserver over /etc/resolv.conf (B4)
    systemd-run --quiet --wait --pipe --collect --uid="$BUILD_USER" --gid="$BUILD_USER" --setenv=HOME=/tmp \
        --property=ProtectSystem=strict --property=ProtectHome=yes --property=PrivateTmp=yes \
        --property=NoNewPrivileges=yes --property=InaccessiblePaths=/etc/awb --property=IPAddressDeny=any \
        --property="IPAddressAllow=$probe_allow" --property="BindReadOnlyPaths=$probe_names/hosts:/etc/hosts" \
        --property="BindReadOnlyPaths=$probe_names/resolv.conf:/etc/resolv.conf" --property=RuntimeMaxSec=60 \
        -- "$@" </dev/null
}

probe_denied() {
    # probe_denied WHAT CMD...: PASS when CMD fails inside the probe unit, and only when the unit was shown to run
    local what="$1"
    shift
    if [ "$dry_run" -eq 1 ]; then
        printf 'CHECK %s\n' "$what"
    elif [ "$probe_ran" -eq 0 ]; then
        report 1 "$what (the probe unit did not run)"
    else
        denied "$what" probe "$@"
    fi
}

build_dry() {
    printf 'CHECK %s\n' "the host file names the build user and the build root" \
        "/etc/awb is root:awb 751, paths.conf and blocklist.txt are 640 root:awb" \
        "the build user exists with the shell /bin/bash and its home in the build root" \
        "the build user is in no group but its own" "the build user has no password" \
        "the build root is mounted from its image, by its fstab line" "the build image is $BUILD_SIZE_GB GB" \
        "main.git is the owner's, bare, 755, and holds main" "wt/ is the build user's, 755" \
        "runs/ is the owner's, 700" "home/ is the build user's, 700" \
        "the venv of the suite is root owned, without a group or other write bit, and imports the test extras" \
        "the home template carries the build settings and CLAUDE.md of the repository and the owner's identity" \
        "the home template, its folder and its three files are immutable" \
        "the build client is the pinned version, root owned, with the pinned digest" \
        "the build client's command runs the pinned version with the updater off" \
        "the build hooks of the release run the gate --no-register by the installed interpreter" \
        "the build runner's unit is installed" \
        "the oneshot $BUILD_FAILED_UNIT of a failed task unit is installed and writes the board line as the owner" \
        "the build side runs on his login (build_model = login) and the login is there, or on the model key" \
        "the model key of a build task is the owner's (400) and matches its source" \
        "the board's build folder is the work user's, 3770" \
        "the build runner refuses its planted cases (awb build selftest)" \
        "the tool guards refuse their planted cases, the build user's too" \
        "the build user cannot see the vault" "the build user cannot read the host file" \
        "the build user cannot read the blocklist" "the build user cannot connect to the check socket" \
        "the build user cannot connect to the key service's call socket" \
        "the build user cannot connect to the vault daemon's admin socket" \
        "the build user cannot connect to the key service's admin socket" \
        "the build user cannot run sudo -n true" "the build user cannot list the work user's home" \
        "the build user cannot list the owner's home" "the build user cannot push into main.git" \
        "the probe unit runs as the build user with the properties of a task" \
        "the probe unit cannot write under $OPT" \
        "the probe unit cannot write under runs/" "the probe unit cannot list runs/" \
        "the probe unit cannot list the work user's home" "the probe unit cannot read the host file" \
        "the probe unit cannot connect to the check socket"
    net_probe
}

net_lines() {
    # net_lines MODEL: the lines of awb build probe (awb/buildrun.py probe_lines), in its order
    printf '%s\n' "the probe unit with a task's own properties runs as the build user" \
        "the probe unit reaches the model endpoint ($MODEL_HOST)"
    [ "$1" != login ] || printf '%s\n' "the probe unit reaches the token host of his login ($TOKEN_HOST)"
    printf '%s\n' "the probe unit cannot reach a TCP API host ($TCP_API_HOST)" "the probe unit cannot reach $GITHUB_HOST" \
        "the probe unit cannot connect to the host's loopback (127.0.0.1:22)" \
        "the probe unit cannot connect to the resolver stub's address (127.0.0.53:22)" \
        "the probe unit cannot connect to the host's own address on port 22"
}

net_probe() {
    # the network of a task through the runner's own code: awb build probe as the owner starts each probe in a unit of
    # the runner's one systemd-run form with the address list of a task's session. Each line counts only as PASS in
    # its output; a missing line is a FAIL
    local model out want
    if [ "$dry_run" -eq 1 ]; then
        while IFS= read -r want; do printf 'CHECK %s\n' "$want"; done < <(net_lines login)
        return 0
    fi
    model=$(conf_value build_model)
    model="${model:-login}"
    out=$(runuser -u "$owner" -- env HOME="$owner_home" /usr/local/bin/awb build probe 2>/dev/null) || true
    while IFS= read -r want; do
        if printf '%s\n' "$out" | grep -qxF -- "PASS  $want"; then report 0 "$want"; else report 1 "$want"; fi
    done < <(net_lines "$model")
}

build_model_check() {
    # build_model_check TEMPLATE: one line names the build model (D-S7 changed). With login: whether the build user's
    # home holds the client's login (its presence only, never its content), the template without apiKeyHelper and no
    # key copy left. With key: the copy of the model key and the template's apiKeyHelper, as in SB3
    local t="$1" model login="$build_root/home/.claude/.credentials.json" out=0
    model=$(conf_value build_model)
    model="${model:-login}"
    case "$model" in
        login)
            grep -q '"apiKeyHelper"' "$t/.claude/settings.json" 2>/dev/null && out=1
            [ ! -e "$BUILD_MODEL_KEY" ] || out=1
            if [ -f "$login" ] && [ ! -L "$login" ] && [ "$(stat -c %U -- "$login")" = "$BUILD_USER" ]; then
                report "$out" "the build side runs on his login (build_model = login): a login is there"
            else
                report 1 "the build side runs on his login (build_model = login): no login yet, as the owner run sudo -u $BUILD_USER -H $BUILD_CLIENT/bin/claude login"
            fi
            ;;
        key)
            grep -q '"apiKeyHelper"' "$t/.claude/settings.json" 2>/dev/null || out=1
            report "$out" "the build side runs on the model key (build_model = key)"
            allowed "the model key of a build task is the owner's (400) and matches its source" sh -c \
                'test -f "$1" && test ! -L "$1" && test "$(stat -c "%U %a" -- "$1")" = "$3 400" && cmp -s -- "$1" "$2"' \
                verify "$(conf_value build_model_key)" "$(conf_value build_model_key_source)" "$owner"
            ;;
        *) report 1 "the build model of the host file is login or key (build_model = $model)" ;;
    esac
}

build_checks() {
    local entry out pin want t f ns addr
    if [ "$dry_run" -eq 1 ]; then
        build_dry
        return 0
    fi
    allowed "the host file names the build user and the build root" sh -c \
        'grep -Eq "^[[:space:]]*build_user[[:space:]]*=[[:space:]]*$2[[:space:]]*$" "$1" &&
         grep -Eq "^[[:space:]]*build_root[[:space:]]*=[[:space:]]*/" "$1"' verify "$CONF_FILE" "$BUILD_USER"
    out=0
    [ "$(stat -c '%U:%G %a' /etc/awb)" = "root:$WORK_GROUP 751" ] || out=1
    [ "$(stat -c '%U:%G %a' "$CONF_FILE")" = "root:$WORK_GROUP 640" ] || out=1
    [ ! -e /etc/awb/blocklist.txt ] || [ "$(stat -c '%U:%G %a' /etc/awb/blocklist.txt)" = "root:$WORK_GROUP 640" ] || out=1
    report "$out" "/etc/awb is root:awb 751, paths.conf and blocklist.txt are 640 root:awb"
    if ! entry=$(getent passwd "$BUILD_USER"); then
        report 1 "the build user exists with the shell /bin/bash and its home in the build root"
        report 1 "the build checks need the build user: run sudo awb deploy"
        return 0
    fi
    out=1
    [ "$(printf '%s\n' "$entry" | cut -d: -f3)" != 0 ] && [ "$(printf '%s\n' "$entry" | cut -d: -f6,7)" = \
        "$build_root/home:/bin/bash" ] && out=0
    report "$out" "the build user exists with the shell /bin/bash and its home in the build root"
    allowed "the build user is in no group but its own" test "$(id -nG "$BUILD_USER")" = "$BUILD_USER"
    allowed "the build user has no password" sh -c 'case "$(getent shadow "$1" | cut -d: -f2)" in "!"* | "*"*) exit 0 ;; esac
        exit 1' verify "$BUILD_USER"
    allowed "the build root is mounted from its image, by its fstab line" sh -c \
        'mountpoint -q "$1" && grep -Eq "^$2[[:space:]]+$1[[:space:]]+ext4[[:space:]]" /etc/fstab &&
         losetup -j "$2" | grep -q .' verify "$build_root" "$BUILD_IMAGE"
    allowed "the build image is $BUILD_SIZE_GB GB" test "$(stat -c %s -- "$BUILD_IMAGE" 2>/dev/null)" = \
        "$((BUILD_SIZE_GB * 1024 * 1024 * 1024))"
    allowed "main.git is the owner's, bare, 755, and holds main" sh -c \
        'test "$(stat -c "%U %a" -- "$1")" = "$2 755" && runuser -u "$2" -- env HOME=/tmp git -C "$1" rev-parse \
         --is-bare-repository | grep -qx true && runuser -u "$2" -- env HOME=/tmp git -C "$1" rev-parse --verify \
         --quiet refs/heads/main >/dev/null' verify "$build_root/main.git" "$owner"
    allowed "wt/ is the build user's, 755" test "$(stat -c '%U %a' -- "$build_root/wt" 2>/dev/null)" = "$BUILD_USER 755"
    allowed "runs/ is the owner's, 700" test "$(stat -c '%U %a' -- "$build_root/runs" 2>/dev/null)" = "$owner 700"
    allowed "home/ is the build user's, 700" test "$(stat -c '%U %a' -- "$build_root/home" 2>/dev/null)" = \
        "$BUILD_USER 700"
    allowed "the venv of the suite is root owned, without a group or other write bit, and imports the test extras" \
        sh -c 'test -z "$(find "$1" ! -user root -print -quit)" && test -z "$(find "$1" ! -type l -perm /022 -print -quit)" &&
        runuser -u "$2" -- "$1/bin/python" -I -B -c "import pytest, pytest_timeout, docx, pptx, openpyxl, reportlab"' \
        verify "$build_root/venv" "$BUILD_USER"
    t="$build_root/home-template"
    allowed "the home template carries the build settings and CLAUDE.md of the repository and the owner's identity" \
        sh -c 'grep -v "^  \"apiKeyHelper\": " "$3/.claude/settings.json" | cmp -s -- "$1/build-claude/settings.json" - &&
        cmp -s -- "$2/CLAUDE.md" "$3/.claude/CLAUDE.md" && grep -Eq "^[[:space:]]+name = .+" "$3/.gitconfig" &&
        grep -Eq "^[[:space:]]+email = .+" "$3/.gitconfig" &&
        ! grep -Eqi "^[[:space:]]*\[safe\]|^[[:space:]]*directory[[:space:]]*=" "$3/.gitconfig"' \
        verify "$seal_dir" "$(dirname -- "$seal_dir")" "$t"
    out=0
    for f in "$t" "$t/.claude" "$t/.claude/settings.json" "$t/.claude/CLAUDE.md" "$t/.gitconfig"; do
        immutable "$f" && [ "$(stat -c %U -- "$f")" = root ] || out=1
    done
    report "$out" "the home template, its folder and its three files are immutable"
    pin=$(sed -n 's/^BUILD_CLIENT_SHA256=\([0-9a-f]\{64\}\).*/\1/p' "$seal_dir/setup.sh")
    want=$(sed -n 's/^BUILD_CLIENT_VERSION=\([0-9.]*\).*/\1/p' "$seal_dir/setup.sh")
    allowed "the build client is the pinned version, root owned, with the pinned digest" sh -c \
        'test "$(stat -c "%U %a" -- "$1")" = "root 755" && test "$(sha256sum <"$1" | cut -d" " -f1)" = "$2"' \
        verify "$BUILD_CLIENT/versions/$want" "$pin"
    allowed "the build client's command runs the pinned version with the updater off" sh -c \
        'test "$(stat -c %U -- "$1")" = root && grep -qx "exec $2 \"\$@\"" "$1" && grep -qx "DISABLE_AUTOUPDATER=1" "$1"' \
        verify "$BUILD_CLIENT/bin/claude" "$BUILD_CLIENT/versions/$want"
    out=0
    for f in pre-commit commit-msg; do
        f="$OPT/src/hooks-build/$f"
        [ -x "$f" ] && [ "$(stat -L -c %U -- "$f")" = root ] && grep -qF -- "--no-register" "$f" &&
            grep -qF -- "$OPT/venv/bin/python3" "$f" || out=1
    done
    report "$out" "the build hooks of the release run the gate --no-register by the installed interpreter"
    allowed "the build runner's unit is installed" test "$(systemctl show -p LoadState --value awb-buildd.service)" = loaded
    allowed "the oneshot $BUILD_FAILED_UNIT of a failed task unit is installed and writes the board line as the owner" \
        sh -c 'test "$(stat -c "%U %a" -- "$1")" = "root 644" && grep -qx "Type=oneshot" "$1" && grep -qx "User=$2" "$1" &&
        grep -qx "ExecStart=/usr/local/bin/awb build failed %i" "$1"' verify "/etc/systemd/system/$BUILD_FAILED_UNIT" "$owner"
    build_model_check "$t"
    allowed "the board's build folder is the work user's, 3770" test \
        "$(stat -c '%U %G %a' -- "$shared/build" 2>/dev/null)" = "awb awb 3770"
    allowed "the build runner refuses its planted cases (awb build selftest)" runuser -u "$owner" -- \
        env HOME="$owner_home" /usr/local/bin/awb build selftest
    allowed "the tool guards refuse their planted cases, the build user's too" as_build /usr/local/bin/awb hook selftest

    # N1 to N5 with the build user's own uid
    denied "the build user cannot see the vault" as_build stat -- "$vault"
    denied "the build user cannot read the host file" as_build cat -- "$CONF_FILE"
    denied "the build user cannot read the blocklist" as_build cat -- /etc/awb/blocklist.txt
    denied "the build user cannot connect to the check socket" as_build python3 -I -c "$CONNECT_PY" "$socket"
    denied "the build user cannot connect to the key service's call socket" as_build python3 -I -c "$CONNECT_PY" \
        /run/awb-keys/cloud.sock
    denied "the build user cannot connect to the vault daemon's admin socket" as_build python3 -I -c "$CONNECT_PY" \
        "$vault/admin.sock"
    denied "the build user cannot connect to the key service's admin socket" as_build python3 -I -c "$CONNECT_PY" \
        "$vault/keys-admin.sock"
    denied "the build user cannot run sudo -n true" as_build sudo -n true
    denied "the build user cannot list the work user's home" as_build ls -A -- "$work_home"
    denied "the build user cannot list the owner's home" as_build ls -A -- "$owner_home"
    denied "the build user cannot push into main.git" as_build sh -c "$PUSH_SH" verify "$build_root/main.git"

    # N2, N4, N5 and N8 inside a unit with the task's properties
    # no nameserver: these units run fixed commands and resolve no name, and /etc/resolv.conf names the stub
    # 127.0.0.53, an address of the host itself (finding of 2026-10-12); like a task's session they see a hosts file
    # and a resolv.conf without a nameserver (B4)
    probe_names=$(mktemp -d)
    trap 'rm -rf -- "$probe_names"' EXIT
    chmod 755 "$probe_names"
    printf '127.0.0.1\tlocalhost\n::1\tlocalhost\n' >"$probe_names/hosts"
    printf '# no nameserver: the unit resolves no name (B4)\n' >"$probe_names/resolv.conf"
    chmod 644 "$probe_names/hosts" "$probe_names/resolv.conf"
    probe_allow=""
    for addr in $(getent ahosts "$MODEL_HOST" | cut -d' ' -f1 | sort -u); do
        probe_allow="$probe_allow $addr"
    done
    probe_allow="${probe_allow# }"
    out=1
    [ -n "$probe_allow" ] && [ "$(probe id -un 2>/dev/null)" = "$BUILD_USER" ] && out=0
    report "$out" "the probe unit runs as the build user with the properties of a task"
    [ "$out" -ne 0 ] || probe_ran=1
    probe_denied "the probe unit cannot write under $OPT" touch -- "$OPT/.verify-probe"
    probe_denied "the probe unit cannot write under runs/" touch -- "$build_root/runs/.verify-probe"
    probe_denied "the probe unit cannot list runs/" ls -A -- "$build_root/runs"
    probe_denied "the probe unit cannot list the work user's home" ls -A -- "$work_home"
    probe_denied "the probe unit cannot read the host file" cat -- "$CONF_FILE"
    probe_denied "the probe unit cannot connect to the check socket" python3 -I -c "$CONNECT_PY" "$socket"
    net_probe
}

build_checks

if [ "$dry_run" -eq 1 ]; then
    exit 0
fi
printf '# %d checks, %d failed\n' "$checks" "$fails"
[ "$fails" -eq 0 ] || exit 1
