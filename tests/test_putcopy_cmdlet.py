"""The secret scrub of awb bucket put keeps a PowerShell cmdlet call: the setup script of task WL broke on
`$secret = New-Object System.Security.SecureString` (2026-11-05)."""

from awb import putcopy

KEY = "pass" + "word"
WORD = "Blue" + "-Horse7"   # a value with no PowerShell verb, built in parts so the gate sees no field


def test_a_cmdlet_call_after_a_secret_variable_is_code_and_stays():
    text = "$secret = New-Object System.Security.SecureString\n$token = Read-Host -AsSecureString\n"
    out, n = putcopy.scrub_text(text)
    assert out == text and n == 0


def test_a_value_that_only_looks_like_a_cmdlet_is_still_replaced():
    """Quoted, after a colon or with no PowerShell verb: a value, replaced."""
    for text, value in ((KEY + ' = "New-Object"\n', "New-Object"), (KEY + ": New-Object\n", "New-Object"),
                        ("sec" "ret = " + WORD + " more\n", WORD)):
        out, n = putcopy.scrub_text(text)
        assert n >= 1 and value not in out, text
