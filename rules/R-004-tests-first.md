# R-004 A test before the code, a control before trust

Every module has tests that fail if it is replaced by a stub. Every checker has a self-test that plants one case per
class and expects exactly that class. A checker whose self-test has not run reports nothing.

Reason: a clean result from a check that cannot fail proves nothing. Enforced by: `build/tests.json`,
`awb gate --selftest`, the pre-commit hook.
