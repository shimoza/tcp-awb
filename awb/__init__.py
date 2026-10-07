"""Architect Workbench (awb).

Runtime code uses the standard library and the poppler command line tools only. Test fixtures may use
python-docx, openpyxl and reportlab. Nothing in this package reads, imports or copies anything from the
old working setup on this host. Customer material enters only through `awb intake`.

The first two statements pin the package to the real folder it was imported from. /opt/tcp-awb/src is a symlink
that `awb deploy` flips to a new release; without the pin a running daemon would import its lazy modules (and
every subpackage, whose __path__ derives from this one) from whatever release is live by then. With it, a process
keeps importing from the release it started on. It must stay here: `awb.tcp` is imported by the dispatcher before
any entry code runs, so a pin called later comes too late (T1, design section 7 item 5).
"""
import os

__path__[0] = os.path.realpath(__path__[0])  # noqa: F821 - the package's own search path

__version__ = "0.1.0"
