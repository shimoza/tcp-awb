"""Arithmetic by a program, never in a reply (R-005).

A model that adds, multiplies or takes a percentage in its head gets it wrong often enough to matter in an offer.
Every number a text computes (a total, a sum, a saving, an average, a product such as 20 x 8) is computed here,
with exact decimal arithmetic, and recorded in the project:

    <project>/calc/calc.tsv    id, time, label, expression, lets, result

An id is K-1, K-2 ... in order. The review accepts a computed number only with `calc:K-N` as its evidence, and only
when the recorded result appears in the claim's sentence (as written, with thousands separators or a decimal comma,
or rounded half up to the places the sentence shows).

Commands (`awb calc ...`):

    awb calc EXPRESSION [--let NAME=EXPRESSION]... [--places N] [--label WORDS] [--project DIR] [--dry]
    awb calc list [--project DIR]
    awb calc show K-N [--project DIR]

The expression is plain arithmetic: numbers, + - * / and ** (a whole exponent up to 64), parentheses and the
functions sum, min, max, abs, round(x, places), ceil, floor and pct(part, whole) (part of whole in percent).
Rounding is half up, as on an invoice. There is no % operator: write a rate as * 0.19. A `--let` binds a name to
an expression that may use the names bound before it. `--places N` rounds the result to N places.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, DecimalException, localcontext
from pathlib import Path

HEADER = ("id", "time", "label", "expression", "lets", "result")
FOLDER = "calc"
FILE = "calc.tsv"
ID_RE = re.compile(r"K-[1-9][0-9]{0,4}")
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9 -]{0,59}")
NAME_RE = re.compile(r"[a-z_][a-z0-9_]{0,31}")
MAX_EXPRESSION = 2000
MAX_NODES = 400
MAX_EXPONENT = 64
MAX_MAGNITUDE = Decimal("1e30")
PRECISION = 40
FUNCTIONS = ("sum", "min", "max", "abs", "round", "ceil", "floor", "pct")


class CalcError(Exception):
    """A calculation that cannot run. The message names the reason, never a value of the record file."""


@dataclass(frozen=True)
class Record:
    id: str
    time: str
    label: str
    expression: str
    lets: str
    result: str

    def value(self) -> Decimal:
        return Decimal(self.result)


# --------------------------------------------------------------------------- evaluation


def _number(text: str) -> Decimal:
    try:
        return Decimal(text)
    except DecimalException:
        raise CalcError("a number cannot be read") from None


def _checked(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) >= MAX_MAGNITUDE:
        raise CalcError("a result is out of range")
    return value


def _round(value: Decimal, places: int) -> Decimal:
    if not 0 <= places <= 12:
        raise CalcError("round takes 0 to 12 places")
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _whole(value: Decimal, what: str) -> int:
    if value != value.to_integral_value():
        raise CalcError("%s must be a whole number" % what)
    return int(value)


def _call(name: str, args: list[Decimal]) -> Decimal:
    if name == "sum":
        return sum(args, Decimal(0))
    if name in ("min", "max"):
        if not args:
            raise CalcError("%s needs at least one value" % name)
        return min(args) if name == "min" else max(args)
    if name == "pct":
        if len(args) != 2:
            raise CalcError("pct takes a part and a whole")
        if args[1] == 0:
            raise CalcError("a division by zero")
        return args[0] / args[1] * 100
    if name == "round":
        if len(args) not in (1, 2):
            raise CalcError("round takes a value and the places")
        return _round(args[0], _whole(args[1], "the places") if len(args) == 2 else 0)
    if len(args) != 1:
        raise CalcError("%s takes one value" % name)
    if name == "abs":
        return abs(args[0])
    return args[0].to_integral_value(rounding=ROUND_CEILING if name == "ceil" else ROUND_FLOOR)


def evaluate(expression: str, names: dict[str, Decimal] | None = None) -> Decimal:
    """The exact value of `expression`. Raises CalcError for anything that is not plain arithmetic."""
    if not isinstance(expression, str) or not expression.strip():
        raise CalcError("an empty expression")
    if len(expression) > MAX_EXPRESSION:
        raise CalcError("the expression is longer than %d characters" % MAX_EXPRESSION)
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError):
        raise CalcError("the expression is not plain arithmetic") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise CalcError("the expression is too long")
    source = expression.strip()
    names = dict(names or {})

    def ev(node) -> Decimal:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CalcError("only numbers are allowed")
            text = ast.get_source_segment(source, node)
            if text is None or not re.fullmatch(r"[0-9][0-9_]*(?:\.[0-9_]*)?(?:[eE][+-]?[0-9]+)?|\.[0-9_]+", text):
                raise CalcError("a number cannot be read")
            return _checked(_number(text.replace("_", "")))
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise CalcError("an unknown name (bind it with --let)")
            return names[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Mod):
                raise CalcError("there is no % operator: write a rate as * 0.19 or use pct(part, whole)")
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return _checked(a + b)
            if isinstance(node.op, ast.Sub):
                return _checked(a - b)
            if isinstance(node.op, ast.Mult):
                return _checked(a * b)
            if isinstance(node.op, ast.Div):
                if b == 0:
                    raise CalcError("a division by zero")
                return _checked(a / b)
            if isinstance(node.op, ast.Pow):
                e = _whole(b, "an exponent")
                if abs(e) > MAX_EXPONENT:
                    raise CalcError("an exponent is larger than %d" % MAX_EXPONENT)
                if a == 0 and e < 0:
                    raise CalcError("a division by zero")
                return _checked(a ** e)
            raise CalcError("only + - * / and ** are allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise CalcError("only the functions %s are allowed" % ", ".join(FUNCTIONS))
            return _checked(_call(node.func.id, [ev(a) for a in node.args]))
        if isinstance(node, (ast.Tuple, ast.List)):
            raise CalcError("a list is allowed only inside a function")
        raise CalcError("the expression is not plain arithmetic")

    with localcontext() as ctx:
        ctx.prec = PRECISION
        try:
            return ev(tree)
        except (DecimalException, ArithmeticError, RecursionError):
            raise CalcError("the expression cannot be computed") from None


def bind(lets: list[str]) -> dict[str, Decimal]:
    """Each NAME=EXPRESSION in order; an expression may use the names bound before it."""
    names: dict[str, Decimal] = {}
    for item in lets:
        name, sep, expr = item.partition("=")
        name = name.strip()
        if not sep or not NAME_RE.fullmatch(name) or name in FUNCTIONS:
            raise CalcError("a --let must read NAME=EXPRESSION with a lowercase name that is not a function")
        if name in names:
            raise CalcError("a name is bound twice")
        names[name] = evaluate(expr, names)
    return names


def text(value: Decimal, places: int | None = None) -> str:
    """The value as plain digits: no exponent, no trailing zeros unless `places` fixes them."""
    if places is not None:
        value = _round(value, places)
        out = format(value, "f")
    else:
        out = format(value.normalize(), "f")
        if "." in out:
            out = out.rstrip("0").rstrip(".")
    return "0" if out in ("-0", "") else out


# --------------------------------------------------------------------------- the record


def path_of(project: Path) -> Path:
    return Path(project) / FOLDER / FILE


def _clean(value: str) -> str:
    return " ".join(str(value).replace("\t", " ").split())


def load(project: Path) -> list[Record]:
    path = path_of(project)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    except OSError:
        raise CalcError("the calculation record cannot be read") from None
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    if not lines or tuple(lines[0].split("\t")) != HEADER:
        raise CalcError("the calculation record has no valid header")
    out = []
    for n, line in enumerate(lines[1:], start=2):
        parts = line.split("\t")
        if len(parts) != len(HEADER) or not ID_RE.fullmatch(parts[0]):
            raise CalcError("line %d of the calculation record is malformed" % n)
        out.append(Record(*parts))
    return out


def find(project: Path, calc_id: str) -> Record | None:
    for r in load(project):
        if r.id == calc_id:
            return r
    return None


def record(project: Path, expression: str, lets: list[str], result: str, label: str = "") -> Record:
    """Append one calculation under the next id. The file is locked while the id is chosen and the line added."""
    folder = Path(project) / FOLDER
    if folder.is_symlink():
        raise CalcError("the calc folder is a link")
    folder.mkdir(exist_ok=True)
    fd = os.open(path_of(project), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_APPEND, 0o660)
    with os.fdopen(fd, "r+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        body = fh.read()
        if not body:
            fh.write("\t".join(HEADER) + "\n")
            body = "\n"
        elif not body.endswith("\n"):
            raise CalcError("the calculation record does not end with a line break")
        last = 0
        for line in body.splitlines()[1:]:
            m = ID_RE.fullmatch(line.split("\t", 1)[0])
            if m:
                last = max(last, int(line.split("\t", 1)[0][2:]))
        rec = Record("K-%d" % (last + 1), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     _clean(label), _clean(expression), _clean("; ".join(lets)), result)
        fh.write("\t".join((rec.id, rec.time, rec.label, rec.expression, rec.lets, rec.result)) + "\n")
    return rec


# --------------------------------------------------------------------------- a result inside a sentence

_WRITTEN_RE = re.compile(r"(?<![\w.,])[0-9]+(?:[.,][0-9]+)*")


def _readings(written: str) -> list[tuple[Decimal, int]]:
    """Every value a written number can mean, with its decimal places: 1,600 is 1600 or 1.6; 1.234,56 is 1234.56."""
    groups = re.split(r"[.,]", written)
    seps = [c for c in written if c in ".,"]
    if not seps:
        return [(Decimal(written), 0)]

    def grouped(parts: list[str]) -> bool:
        return 1 <= len(parts[0]) <= 3 and all(len(g) == 3 for g in parts[1:])

    out: list[tuple[Decimal, int]] = []
    if len(set(seps)) == 1 and grouped(groups):
        out.append((Decimal("".join(groups)), 0))
    head, tail, last = groups[:-1], groups[-1], seps[-1]
    if all(c != last for c in seps[:-1]) and len(set(seps[:-1])) <= 1 and (len(head) == 1 or grouped(head)):
        out.append((Decimal("".join(head) + "." + tail), len(tail)))
    return out


def appears_in(result: Decimal, sentence: str) -> bool:
    """True when `sentence` states `result` (its size, without a sign), exactly or rounded half up to the places
    the sentence shows."""
    target = abs(result)
    for m in _WRITTEN_RE.finditer(sentence):
        for value, places in _readings(m.group(0)):
            if value == target:
                return True
            if value != 0 and _round(target, places) == value:
                return True
    return False


# --------------------------------------------------------------------------- command line


def _project(arg: str | None) -> Path:
    from awb import review

    return Path(arg) if arg else review.find_project()


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("list", "show"):
        ap = SafeParser(prog="awb calc", description="The calculations recorded in a project.")
        sub = ap.add_subparsers(dest="command")
        s = sub.add_parser("list", help="every recorded calculation")
        s.add_argument("--project", default=None)
        s = sub.add_parser("show", help="one recorded calculation")
        s.add_argument("id")
        s.add_argument("--project", default=None)
    else:
        ap = SafeParser(prog="awb calc", description="Compute with exact decimals and record it in the project.")
        ap.add_argument("expression")
        ap.add_argument("--let", action="append", default=[], metavar="NAME=EXPRESSION")
        ap.add_argument("--places", type=int, default=None)
        ap.add_argument("--label", default="")
        ap.add_argument("--project", default=None)
        ap.add_argument("--dry", action="store_true", help="compute without recording")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        if getattr(args, "command", None) == "list":
            for r in load(_project(args.project)):
                print("%s  %s = %s%s" % (r.id, r.expression, r.result, "  (%s)" % r.label if r.label else ""))
            return 0
        if getattr(args, "command", None) == "show":
            if not ID_RE.fullmatch(args.id):
                print("awb calc: an id reads K-N", file=sys.stderr)
                return 2
            r = find(_project(args.project), args.id)
            if r is None:
                print("awb calc: no such calculation in this project", file=sys.stderr)
                return 1
            print("%s  %s\n  lets: %s\n  expression: %s\n  result: %s\n  label: %s"
                  % (r.id, r.time, r.lets or "-", r.expression, r.result, r.label or "-"))
            return 0
        if args.label and not LABEL_RE.fullmatch(args.label):
            print("awb calc: a label is lowercase letters, digits, spaces and hyphens, up to 60", file=sys.stderr)
            return 2
        if args.places is not None and not 0 <= args.places <= 12:
            print("awb calc: --places takes 0 to 12", file=sys.stderr)
            return 2
        value = evaluate(args.expression, bind(args.let))
        result = text(value, args.places)
        if args.dry:
            print("%s = %s  (not recorded)" % (_clean(args.expression), result))
            return 0
        project = _project(args.project)
        if not (project / "SCOPE.md").is_file():
            print("awb calc: not inside a project (no SCOPE.md); use --project or --dry", file=sys.stderr)
            return 2
        rec = record(project, args.expression, args.let, result, args.label)
        print("%s  %s = %s" % (rec.id, rec.expression, rec.result))
        print("evidence for the review: calc:%s" % rec.id)
        return 0
    except CalcError as err:
        print("awb calc: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb calc: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
