"""Task SU: `seal/setup.sh --update` (what `sudo awb deploy` runs) installs the units of the work jobs too. Before, a
changed timer of seal/work-jobs (awb-kb-recheck.timer, task KS) reached the host only with a full seal and had to be
installed by hand. The update installs a unit only where its content differs, reloads systemd once and enables and
restarts only the timers that changed; a service is never started and an unchanged unit causes nothing."""
from __future__ import annotations

import os
import re

from tests.test_seal import SETUP, WORK_JOBS, _persist_run, commands, fakebin, no_marks, run_script, setup_value  # noqa: F401

UNITS = [n + "." + k for n in setup_value("WORK_JOBS").split() for k in ("service", "timer")]
TIMERS = [u for u in UNITS if u.endswith(".timer")]


def test_su_the_update_path_calls_the_work_unit_step_after_the_units(fakebin):
    """update_steps names step_work_units right after step_units. Its dry run plans an install of each of the six
    units where it differs, one daemon-reload and an enable --now and a try-restart of each timer where it changed; no
    service is enabled or started, cron.deny and the crontab stay out. --units-only leaves it out."""
    text = SETUP.read_text(encoding="utf-8")
    assert re.search(r"^update_steps\(\) \{\n(    #.*\n)?    if \[ \"\$units_only\" -eq 1 \]; then\n        step_units\n"
                     r"        return 0\n    fi\n    step_code\n    step_conf\n    step_units\n    step_work_units\n"
                     r"    step_egress\n    step_needrestart\n    step_holds\n    step_kb_hooks\n    step_client\n"
                     r"    step_managed\n    step_build\n    step_mirrors\n\}", text, re.M)
    res = run_script(SETUP, ["--update", "--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    cmds = commands(res.stdout)
    for unit in UNITS:
        assert "+ install -o root -g root -m 0644 <rendered> /etc/systemd/system/%s  # if it differs" % unit in cmds
    assert "+ systemctl daemon-reload  # if a unit changed" in cmds
    for timer in TIMERS:
        assert "+ systemctl enable --now %s  # if it changed" % timer in cmds
        assert "+ systemctl try-restart %s  # if it changed" % timer in cmds
    work = [c for c in cmds if any(u in c for u in UNITS)]
    assert not [c for c in work if " start " in c or " restart " in c or ".service  # if it changed" in c]
    assert not [c for c in cmds if "cron" in c]
    assert cmds.index("+ systemctl daemon-reload  # if a unit changed") > max(
        cmds.index("+ install -o root -g root -m 0644 <rendered> /etc/systemd/system/%s  # if it differs" % u)
        for u in UNITS)
    only = run_script(SETUP, ["--update", "--units-only", "--dry-run"], fakebin)
    assert only.returncode == 0, only.stderr
    assert not [c for c in commands(only.stdout) if any(u in c for u in UNITS)]


def test_su_an_unchanged_unit_is_not_rewritten_and_causes_no_reload_or_restart(tmp_path):
    """With fakes of install and systemctl: the first update installs the six units, reloads once and enables and
    restarts the three timers. A second update finds every unit as rendered: no file is written (its time stays),
    systemctl is not called at all."""
    res, calls = _persist_run(tmp_path, "step_work_units\n")
    assert res.returncode == 0, res.stderr + res.stdout
    for unit in UNITS:
        text = (tmp_path / "units" / unit).read_text()
        assert "@" not in text, unit
        assert text == (WORK_JOBS / unit).read_text().replace("@WORK_HOME@", "%s/work" % tmp_path).replace(
            "@KB_COMMIT@", "%s/work/.local/bin/nightly-kb-commit" % tmp_path), unit
    assert calls == ["systemctl daemon-reload"] + [
        c for t in TIMERS for c in ("systemctl enable --now %s" % t, "systemctl try-restart %s" % t)]
    old = 1_600_000_000_000_000_000
    for unit in UNITS:
        os.utime(tmp_path / "units" / unit, ns=(old, old))
    (tmp_path / "calls.log").unlink()
    res, calls = _persist_run(tmp_path, "step_work_units\n")
    assert res.returncode == 0, res.stderr + res.stdout
    assert calls == []
    for unit in UNITS:
        assert (tmp_path / "units" / unit).stat().st_mtime_ns == old, unit
        assert "= install -o root -g root -m 0644 <rendered> %s/units/%s  # unchanged" % (tmp_path, unit) in res.stdout
    assert "= systemctl daemon-reload  # no unit changed" in res.stdout


def test_su_a_changed_timer_is_reinstalled_and_restarted_and_nothing_else(tmp_path):
    """The case of 2026-10-28: the installed awb-kb-recheck.timer still fires Sunday 02:30 UTC. The update writes
    the rendered timer over it, reloads once and enables and restarts that timer alone; the other units keep their
    files and their time. A changed service alone gets the reload and no restart: its timer starts it."""
    res, _ = _persist_run(tmp_path, "step_work_units\n")
    assert res.returncode == 0, res.stderr + res.stdout
    stale = tmp_path / "units" / "awb-kb-recheck.timer"
    stale.write_text(stale.read_text().replace("OnCalendar=Sat *-*-* 19:00:00 UTC", "OnCalendar=Sun *-*-* 02:30:00 UTC"))
    assert "Sun *-*-* 02:30:00" in stale.read_text()
    old = 1_600_000_000_000_000_000
    for unit in UNITS:
        os.utime(tmp_path / "units" / unit, ns=(old, old))
    (tmp_path / "calls.log").unlink()
    res, calls = _persist_run(tmp_path, "step_work_units\n")
    assert res.returncode == 0, res.stderr + res.stdout
    assert "OnCalendar=Sat *-*-* 19:00:00 UTC" in stale.read_text()
    assert calls == ["systemctl daemon-reload", "systemctl enable --now awb-kb-recheck.timer",
                     "systemctl try-restart awb-kb-recheck.timer"]
    for unit in UNITS:
        assert ((tmp_path / "units" / unit).stat().st_mtime_ns == old) is (unit != "awb-kb-recheck.timer"), unit
    service = tmp_path / "units" / "awb-kb-recheck.service"
    service.write_text(service.read_text() + "# planted\n")
    (tmp_path / "calls.log").unlink()
    res, calls = _persist_run(tmp_path, "step_work_units\n")
    assert res.returncode == 0, res.stderr + res.stdout
    assert "# planted" not in service.read_text()
    assert calls == ["systemctl daemon-reload"]
