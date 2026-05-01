"""Tests for zerodha/zerodha_connect.py — KiteConnect REST client."""
import hashlib
import pytest
from unittest.mock import MagicMock, patch
from zerodha.zerodha_connect import KiteConnect
import kiteconnect.exceptions as ex


# ── Helpers ───────────────────────────────────────────────────────────────────

def _kite(api_key="test_api_key"):
    return KiteConnect(api_key=api_key)


def _mock_response(status_code=200, json_data=None, content_type="application/json", content=b""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": content_type}
    resp.content = content
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


def _ok_response(data):
    return _mock_response(json_data={"status": "success", "data": data})


def _error_response(status_code, error_type, message="error"):
    return _mock_response(
        status_code=status_code,
        json_data={"status": "error", "error_type": error_type, "message": message},
    )


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestInit:
    def test_api_key_stored(self):
        kite = _kite("my_key")
        assert kite.api_key == "my_key"

    def test_default_root_is_kite_trade(self):
        kite = _kite()
        assert "kite.trade" in kite.root

    def test_default_timeout_is_7(self):
        kite = _kite()
        assert kite.timeout == 7

    def test_access_token_none_by_default(self):
        kite = _kite()
        assert kite.access_token is None

    def test_custom_root_stored(self):
        kite = KiteConnect(api_key="k", root="https://custom.host")
        assert kite.root == "https://custom.host"

    def test_custom_timeout_stored(self):
        kite = KiteConnect(api_key="k", timeout=15)
        assert kite.timeout == 15


# ── set_session_expiry_hook ───────────────────────────────────────────────────

class TestSessionExpiryHook:
    def test_callable_accepted(self):
        kite = _kite()
        hook = lambda: None
        kite.set_session_expiry_hook(hook)
        assert kite.session_expiry_hook is hook

    def test_non_callable_raises_type_error(self):
        kite = _kite()
        with pytest.raises(TypeError):
            kite.set_session_expiry_hook("not_a_function")

    def test_none_raises_type_error(self):
        kite = _kite()
        with pytest.raises(TypeError):
            kite.set_session_expiry_hook(None)


# ── set_access_token / update_enctoken ───────────────────────────────────────

class TestTokenSetters:
    def test_set_access_token(self):
        kite = _kite()
        kite.set_access_token("tok123")
        assert kite.access_token == "tok123"

    def test_update_enctoken(self):
        kite = _kite()
        kite.update_enctoken("enc_abc")
        assert kite.enc_token == "enc_abc"


# ── login_url ─────────────────────────────────────────────────────────────────

class TestLoginUrl:
    def test_contains_api_key(self):
        kite = KiteConnect(api_key="mykey123")
        url = kite.login_url()
        assert "mykey123" in url

    def test_contains_version_param(self):
        kite = _kite()
        url = kite.login_url()
        assert "v=" in url

    def test_is_string(self):
        kite = _kite()
        assert isinstance(kite.login_url(), str)


# ── generate_session ──────────────────────────────────────────────────────────

class TestGenerateSession:
    def test_checksum_is_sha256_of_key_request_secret(self):
        api_key = "apikey"
        request_token = "reqtok"
        api_secret = "secret"
        expected = hashlib.sha256(
            (api_key + request_token + api_secret).encode("utf-8")
        ).hexdigest()

        kite = KiteConnect(api_key=api_key)
        captured = {}

        def fake_post(route, params=None, **kwargs):
            captured["params"] = params
            return {"access_token": "tok", "login_time": ""}

        kite._post = fake_post
        kite.generate_session(request_token, api_secret)
        assert captured["params"]["checksum"] == expected

    def test_access_token_set_from_response(self):
        kite = _kite()
        kite._post = lambda *a, **kw: {"access_token": "generated_tok", "login_time": ""}
        kite.generate_session("rt", "secret")
        assert kite.access_token == "generated_tok"

    def test_no_access_token_in_response_does_not_crash(self):
        kite = _kite()
        kite._post = lambda *a, **kw: {"login_time": ""}
        kite.generate_session("rt", "secret")   # should not raise
        assert kite.access_token is None


# ── _request error routing ────────────────────────────────────────────────────

class TestRequestErrorRouting:
    """Verify that Kite error_type strings map to the correct exception classes."""

    def _call(self, kite, error_type, status_code):
        """Trigger _request with an error response and return the raised exception."""
        resp = _error_response(status_code, error_type)
        kite.reqsession.request = MagicMock(return_value=resp)
        with pytest.raises(Exception) as exc_info:
            kite._request("user.profile", "GET")
        return exc_info.value

    def test_token_exception_403(self):
        kite = _kite()
        exc = self._call(kite, "TokenException", 403)
        assert isinstance(exc, ex.TokenException)
        assert exc.code == 403

    def test_permission_exception_403(self):
        kite = _kite()
        exc = self._call(kite, "PermissionException", 403)
        assert isinstance(exc, ex.PermissionException)

    def test_input_exception_400(self):
        kite = _kite()
        exc = self._call(kite, "InputException", 400)
        assert isinstance(exc, ex.InputException)
        assert exc.code == 400

    def test_data_exception_502(self):
        kite = _kite()
        exc = self._call(kite, "DataException", 502)
        assert isinstance(exc, ex.DataException)

    def test_network_exception_503(self):
        kite = _kite()
        exc = self._call(kite, "NetworkException", 503)
        assert isinstance(exc, ex.NetworkException)

    def test_unknown_error_type_raises_general_exception(self):
        kite = _kite()
        exc = self._call(kite, "SomeFutureException", 500)
        assert isinstance(exc, ex.GeneralException)

    def test_session_hook_called_on_token_exception_403(self):
        kite = _kite()
        hook = MagicMock()
        kite.set_session_expiry_hook(hook)
        resp = _error_response(403, "TokenException")
        kite.reqsession.request = MagicMock(return_value=resp)
        with pytest.raises(ex.TokenException):
            kite._request("user.profile", "GET")
        hook.assert_called_once()

    def test_session_hook_not_called_on_non_403(self):
        kite = _kite()
        hook = MagicMock()
        kite.set_session_expiry_hook(hook)
        resp = _error_response(400, "InputException")
        kite.reqsession.request = MagicMock(return_value=resp)
        with pytest.raises(ex.InputException):
            kite._request("user.profile", "GET")
        hook.assert_not_called()

    def test_unknown_content_type_raises_data_exception(self):
        kite = _kite()
        resp = _mock_response(status_code=200, content_type="text/html", content=b"<html/>")
        kite.reqsession.request = MagicMock(return_value=resp)
        with pytest.raises(ex.DataException):
            kite._request("user.profile", "GET")

    def test_successful_response_returns_data(self):
        kite = _kite()
        resp = _ok_response({"profile": "ok"})
        kite.reqsession.request = MagicMock(return_value=resp)
        result = kite._request("user.profile", "GET")
        assert result == {"profile": "ok"}


# ── _parse_instruments (CSV parsing) ─────────────────────────────────────────

class TestParseInstruments:
    _CSV = (
        "instrument_token,exchange_token,tradingsymbol,name,last_price,"
        "expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\r\n"
        "256265,1001,NIFTY 50,NIFTY,22100.5,,0.0,0.05,1,INDEX,INDICES,NSE\r\n"
        "1234,5678,RELIANCE,RELIANCE INDUSTRIES,2900.0,,0.0,0.05,1,EQ,NSE,NSE\r\n"
    )

    def test_returns_list_of_dicts(self):
        kite = _kite()
        kite._get = MagicMock(return_value=self._CSV.encode())
        result = kite.instruments()
        assert isinstance(result, list)
        assert len(result) == 2

    def test_instrument_token_is_int(self):
        kite = _kite()
        kite._get = MagicMock(return_value=self._CSV.encode())
        result = kite.instruments()
        assert isinstance(result[0]["instrument_token"], int)

    def test_last_price_is_float(self):
        kite = _kite()
        kite._get = MagicMock(return_value=self._CSV.encode())
        result = kite.instruments()
        assert isinstance(result[0]["last_price"], float)

    def test_lot_size_is_int(self):
        kite = _kite()
        kite._get = MagicMock(return_value=self._CSV.encode())
        result = kite.instruments()
        assert isinstance(result[0]["lot_size"], int)

    def test_strike_is_float(self):
        kite = _kite()
        kite._get = MagicMock(return_value=self._CSV.encode())
        result = kite.instruments()
        assert isinstance(result[0]["strike"], float)

    def test_tradingsymbol_present(self):
        kite = _kite()
        kite._get = MagicMock(return_value=self._CSV.encode())
        result = kite.instruments()
        symbols = [r["tradingsymbol"] for r in result]
        assert "NIFTY 50" in symbols
        assert "RELIANCE" in symbols
