"""Tenant-context helpers: database error classification."""

from django.db.utils import ProgrammingError
from django.test import SimpleTestCase
from psycopg.errors import UndefinedFunction, UndefinedTable

from organizations.context import is_missing_function


def _wrapped(error):
    try:
        try:
            raise error
        except Exception as exc:
            raise ProgrammingError(str(exc)) from exc
    except ProgrammingError as wrapped:
        return wrapped
    raise AssertionError("unreachable")


class MissingFunctionTests(SimpleTestCase):
    def test_psycopg3_undefined_function_matches(self):
        # Live worker traceback: SELECT offerflow_run_org(n) on a
        # database without the helper raises SQLSTATE 42883, which
        # psycopg3 reports as `sqlstate` — there is no `pgcode`.
        wrapped = _wrapped(
            UndefinedFunction("function offerflow_run_org(1) does not exist")
        )
        self.assertTrue(is_missing_function(wrapped))

    def test_psycopg2_pgcode_still_matches(self):
        cause = Exception("function x() does not exist")
        cause.pgcode = "42883"  # psycopg2-style attribute
        wrapped = ProgrammingError(str(cause))
        wrapped.__cause__ = cause
        self.assertTrue(is_missing_function(wrapped))

    def test_other_database_errors_do_not_match(self):
        wrapped = _wrapped(UndefinedTable('relation "x" does not exist'))
        self.assertFalse(is_missing_function(wrapped))

    def test_plain_errors_do_not_match(self):
        self.assertFalse(is_missing_function(ValueError("nope")))
