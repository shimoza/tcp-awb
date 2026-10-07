#!/usr/bin/env bash
# The switch of the web side to the code of this repository (seal/web/README.md). Run it with sudo as the owner,
# after seal/setup.sh installed the current code into /opt/tcp-awb:
#
#   sudo seal/web/install.sh [--domain <host name of the site>] [--only UNIT...] [--dry-run]
#   sudo seal/web/install.sh --rollback [--dry-run]
#
# --domain may be left out once the installed awb-web.service carries one: the script reads it from there.
# --only UNIT... (what `sudo awb deploy` passes, run from the release folder it extracted) restarts the named units
# only, in the order of the restart lines below; the render, the daemon-reload and the "unchanged" check stay.
#
# It creates the system user awb-console once (F2), renders every template of this folder with the values of
# /etc/awb/paths.conf, keeps each unit it replaces as <unit>.before-switch (an earlier copy stays), retires the
# drop-in awb-ask.service.d/90-awb-web.conf (renamed to .off, systemd reads .conf only), reloads systemd and
# restarts the key service and the web units. The restarted key service holds no key: the owner runs
# `awb keys unlock` right after. --dry-run prints every step and changes nothing.
#
# --rollback goes back: every <unit>.before-switch over its unit, a unit the switch added renamed to .off, the old
# drop-in back, systemd reloaded and the web units restarted on the copies in /opt/awb-web, which the switch never
# touches. The page of the console has its own backup (/srv/awb-web/index.html.before-switch).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
CONF=${AWB_INSTALL_CONF:-/etc/awb/paths.conf}
UNITS=${AWB_INSTALL_UNITS:-/etc/systemd/system}
CONSOLE_USER=awb-console
dry=0
rollback=0
domain=""
only=""

die() { echo "install.sh: $*" >&2; exit 2; }
run() {
    printf '+'; printf ' %q' "$@"; printf '\n'
    [ "$dry" -eq 1 ] || "$@"
}

usage="usage: sudo seal/web/install.sh [--domain HOST] [--only UNIT...] [--dry-run] | --rollback [--dry-run]"
while [ $# -gt 0 ]; do
    case "$1" in
        --domain) domain="${2:-}"; shift 2 ;;
        --dry-run) dry=1; shift ;;
        --rollback) rollback=1; shift ;;
        --only)
            shift
            while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do
                [[ "$1" =~ ^awb-[a-z0-9-]+\.(service|socket)$ ]] || die "--only takes unit names"
                only="$only $1"
                shift
            done
            [ -n "$only" ] || die "--only needs at least one unit"
            ;;
        *) die "$usage" ;;
    esac
done
[ -z "$only" ] || [ "$rollback" -eq 0 ] || die "--only does not go with --rollback"

# restart CMD UNIT...: systemctl CMD on the units, with --only on the named ones only (in this order)
restart() {
    local cmd="$1" u keep=()
    shift
    if [ "$cmd" = enable ]; then
        cmd="enable --now"
        shift
    fi
    for u in "$@"; do
        if [ -z "$only" ] || [[ " $only " == *" $u "* ]]; then
            keep+=("$u")
        fi
    done
    [ "${#keep[@]}" -gt 0 ] || return 0
    run systemctl $cmd "${keep[@]}"
}
cd "$here"
templates=$(find . -mindepth 1 -maxdepth 2 -type f \( -name '*.service' -o -name '*.socket' -o -name '*.conf' \) | sort)
[ -n "$templates" ] || die "no template found next to this script"
old_dropin="$UNITS/awb-ask.service.d/90-awb-web.conf"

if [ "$rollback" -eq 1 ]; then
    for rel in $templates; do
        rel=${rel#./}
        target="$UNITS/$rel"
        if [ -e "$target.before-switch" ]; then
            run mv "$target.before-switch" "$target"
        elif [ -f "$target" ]; then
            [ "$rel" != "awb-console-tenants.service" ] || run systemctl disable --now awb-console-tenants.service
            run mv "$target" "$target.off"
        fi
    done
    if [ -f "$old_dropin.off" ]; then
        run mv "$old_dropin.off" "$old_dropin"
    fi
    run systemctl daemon-reload
    run systemctl stop awb-customers.service awb-project-create.service awb-materials.service
    run systemctl restart awb-customers.socket awb-project-create.socket awb-materials.socket
    run systemctl restart awb-portal.service awb-ask.service awb-console-data.service awb-web.service
    echo "rolled back: the web units run the copies in /opt/awb-web again"
    exit 0
fi

if [ -z "$domain" ] && [ -f "$UNITS/awb-web.service" ]; then
    domain=$(sed -n 's/^ExecStart=.*--domain[[:space:]]\{1,\}\([^[:space:]]*\).*/\1/p' "$UNITS/awb-web.service" | head -n 1)
    [ -z "$domain" ] || echo "the site $domain, as the installed awb-web.service names it"
fi
[ -n "$domain" ] || die "--domain names the host name of the site, without scheme"
[[ "$domain" =~ ^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$ ]] || die "the domain is a plain host name in lower case"
[ -r "$CONF" ] || die "$CONF cannot be read: run seal/setup.sh first"

conf() { sed -n "s/^$1[[:space:]]*=[[:space:]]*//p" "$CONF" | head -n 1; }
owner=$(conf owner)
work_user=$(conf work_user)
vault=$(conf vault)
shared=$(conf shared)
[ -n "$owner" ] && [ -n "$work_user" ] && [ -n "$vault" ] && [ -n "$shared" ] ||
    die "$CONF names no owner, work_user, vault or shared"
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "$owner" ]; then
    die "run this with sudo as the owner of $CONF"
fi
work_home=$(getent passwd "$work_user" | cut -d: -f6)
work_group=$(id -gn "$work_user")
[ -n "$work_home" ] && [ -n "$work_group" ] || die "the work user $work_user is not on this host"
for value in "$owner" "$vault" "$shared" "$work_home"; do
    [[ "$value" =~ ^[A-Za-z0-9/._-]+$ ]] || die "a value of $CONF holds a character a unit file cannot take"
done

echo "the web side from /opt/tcp-awb: owner $owner, work user $work_user, site $domain"

if id "$CONSOLE_USER" >/dev/null 2>&1; then
    echo "the system user $CONSOLE_USER exists"
else
    run useradd --system --gid "$work_group" --no-create-home -d /nonexistent --shell /usr/sbin/nologin \
        "$CONSOLE_USER"
fi

render() {
    sed -e "s|@OWNER@|$owner|g" -e "s|@DOMAIN@|$domain|g" -e "s|@VAULT@|$vault|g" -e "s|@SHARED@|$shared|g" \
        -e "s|@WORK_HOME@|$work_home|g" "$1"
}

for rel in $templates; do
    rel=${rel#./}
    target="$UNITS/$rel"
    text=$(render "$rel")
    if printf '%s\n' "$text" | grep -q '@[A-Z_]*@'; then
        die "$rel keeps a placeholder after rendering"
    fi
    if [ -f "$target" ] && [ "$(cat "$target")" = "$text" ]; then
        echo "unchanged $target"
        continue
    fi
    run mkdir -p "$(dirname "$target")"
    if [ -f "$target" ] && [ ! -e "$target.before-switch" ]; then
        run cp -p "$target" "$target.before-switch"
    fi
    echo "write $target"
    if [ "$dry" -eq 0 ]; then
        printf '%s\n' "$text" > "$target.next"
        chmod 644 "$target.next"
        mv "$target.next" "$target"
    fi
done

if [ -f "$old_dropin" ]; then
    run mv "$old_dropin" "$old_dropin.off"
fi

run systemctl daemon-reload
restart restart awb-keyd.service
restart stop awb-customers.service awb-project-create.service awb-materials.service
restart restart awb-customers.socket awb-project-create.socket awb-materials.socket
restart restart awb-portal.service awb-ask.service awb-console-data.service
restart enable --now awb-console-tenants.service
restart restart awb-console-tenants.service awb-web.service
if [ -z "$only" ] || [[ " $only " == *" awb-keyd.service "* ]]; then
    echo "done: as $owner, run awb keys unlock now; the key service restarted and holds no key"
else
    echo "done"
fi
