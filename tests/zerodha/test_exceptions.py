"""Tests for zerodha/zerodha_exceptions.py — exception hierarchy."""
import pytest
from zerodha.zerodha_exceptions import (
    KiteException,
    GeneralException,
    TokenException,
    PermissionException,
    OrderException,
    InputException,
    DataException,
    NetworkException,
)


# ── Inheritance ───────────────────────────────────────────────────────────────

class TestInheritance:
    def test_general_exception_is_kite_exception(self):
        assert issubclass(GeneralException, KiteException)

    def test_token_exception_is_kite_exception(self):
        assert issubclass(TokenException, KiteException)

    def test_permission_exception_is_kite_exception(self):
        assert issubclass(PermissionException, KiteException)

    def test_order_exception_is_kite_exception(self):
        assert issubclass(OrderException, KiteException)

    def test_input_exception_is_kite_exception(self):
        assert issubclass(InputException, KiteException)

    def test_data_exception_is_kite_exception(self):
        assert issubclass(DataException, KiteException)

    def test_network_exception_is_kite_exception(self):
        assert issubclass(NetworkException, KiteException)

    def test_kite_exception_is_python_exception(self):
        assert issubclass(KiteException, Exception)


# ── Default codes ─────────────────────────────────────────────────────────────

class TestDefaultCodes:
    def test_kite_exception_default_code_500(self):
        exc = KiteException("msg")
        assert exc.code == 500

    def test_general_exception_default_code_500(self):
        assert GeneralException("msg").code == 500

    def test_token_exception_default_code_403(self):
        assert TokenException("msg").code == 403

    def test_permission_exception_default_code_403(self):
        assert PermissionException("msg").code == 403

    def test_order_exception_default_code_500(self):
        assert OrderException("msg").code == 500

    def test_input_exception_default_code_400(self):
        assert InputException("msg").code == 400

    def test_data_exception_default_code_502(self):
        assert DataException("msg").code == 502

    def test_network_exception_default_code_503(self):
        assert NetworkException("msg").code == 503


# ── Code override ─────────────────────────────────────────────────────────────

class TestCodeOverride:
    def test_custom_code_stored(self):
        exc = TokenException("expired", code=401)
        assert exc.code == 401

    def test_message_accessible_via_str(self):
        exc = InputException("bad param")
        assert "bad param" in str(exc)

    def test_message_accessible_via_args(self):
        exc = DataException("parse error")
        assert exc.args[0] == "parse error"


# ── Raise / catch ─────────────────────────────────────────────────────────────

class TestRaiseCatch:
    def test_raise_and_catch_as_kite_exception(self):
        with pytest.raises(KiteException):
            raise TokenException("token expired")

    def test_raise_and_catch_as_concrete_type(self):
        with pytest.raises(InputException):
            raise InputException("missing field")

    def test_catch_general_does_not_catch_token(self):
        with pytest.raises(TokenException):
            try:
                raise TokenException("auth failed")
            except GeneralException:
                pytest.fail("Should not be caught as GeneralException")

    def test_network_exception_caught_as_exception(self):
        with pytest.raises(Exception):
            raise NetworkException("timeout")
