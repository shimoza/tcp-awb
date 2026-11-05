"""Two limits of Windows PowerShell the text checks of B-036 missed; both broke setup.ps1 on the lab laptop on
2026-11-05."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [ROOT / "awb" / "winlab" / "setup.ps1", ROOT / "awb" / "winlab" / "remove.ps1"]


def test_a_local_account_description_has_at_most_48_characters():
    for f in SCRIPTS:
        for d in re.findall(r"-Description '([^']*)'", f.read_text(encoding="utf-8")):
            assert len(d) <= 48, (f.name, d)


def _plus_outside_parentheses(line: str) -> bool:
    depth, quote = 0, ""
    for i, ch in enumerate(line):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "+" and depth <= 0 and line[i - 1:i + 2] == " + ":
            return True
    return False


def test_an_array_element_built_with_plus_stands_in_parentheses():
    """In PowerShell the comma binds before +: 'a' + $x + 'b', 'c' is 'a' + $x + ('b', 'c'), one joined string."""
    for f in SCRIPTS:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if s.endswith(",") and not s.startswith("#") and _plus_outside_parentheses(s):
                raise AssertionError("%s:%d: put the element in parentheses" % (f.name, n))
