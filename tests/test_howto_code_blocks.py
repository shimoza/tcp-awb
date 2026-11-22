"""tcp-bytg ask 3 (2026-11-22): awb howto render renders fenced code blocks. A block of three backticks and a
language came out as one paragraph with the backticks and the language word, its line breaks lost and a star pair
turned into italics, so the HTML copy of a guide could not be offered for copying commands."""
from __future__ import annotations

import html
import re

from awb.tcp import howto

GUIDE = """# Guide for CUST-Q7M4

1. Open PowerShell as administrator.
2. Run the check:

   ```powershell
   Get-ChildItem -Path C:\\Lab\\*.vhdx | Select-Object Name, Length
   $here = @"
   line <one> & *two*
   "@
   ```

3. Read the result.

~~~
awb pdf deliverables/guide-q7m4.md
~~~
"""


def blocks(page: str) -> list[tuple[str, str]]:
    return [(lang, html.unescape(text)) for lang, text in
            re.findall(r'<pre><code(?: class="language-([\w+#.-]+)")?>(.*?)</code></pre>', page, re.S)]


def test_bytg_a_fenced_block_with_a_language_keeps_its_lines_and_no_markup(tmp_path):
    page, problems = howto.to_html(GUIDE, tmp_path)
    assert not problems
    got = blocks(page)
    assert got[0] == ("powershell", 'Get-ChildItem -Path C:\\Lab\\*.vhdx | Select-Object Name, Length\n'
                                    '$here = @"\nline <one> & *two*\n"@')
    assert got[1] == ("", "awb pdf deliverables/guide-q7m4.md")
    assert "```" not in page and "~~~" not in page and "<em>two</em>" not in page
    assert "&lt;one&gt; &amp;" in page                        # escaped, never markup


def test_bytg_a_block_under_a_step_stays_in_the_list_and_the_steps_go_on(tmp_path):
    page, _ = howto.to_html(GUIDE, tmp_path)
    body = page.split("<body>", 1)[1]
    assert body.count("<ol>") == 1                            # step 3 continues the same list
    pre = body.index("<pre>")
    assert body.index("<li>Run the check:") < pre < body.index("<li>Read the result.") < body.index("</ol>")
    assert body.index("</ol>") < body.rindex("<pre>")         # the unindented block comes after the list


def test_bytg_a_block_without_a_closing_fence_ends_with_the_draft_and_render_writes_it(tmp_path):
    draft = tmp_path / "howto-q7m4.md"
    draft.write_text("# Guide\n\nText.\n\n```bash\nawb gate --selftest\n", encoding="utf-8")
    out, problems, _ = howto.render(draft)
    page = out.read_text(encoding="utf-8")
    assert not problems and blocks(page) == [("bash", "awb gate --selftest")]
    assert "<p>Text.</p>" in page
