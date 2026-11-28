"""Tests for scripts/sensibull_login.py orchestration (no browser, no network)."""
from __future__ import annotations

import pytest

import scripts.sensibull_login as login_script

COOKIES = {"access_token": "tok", "client_info": "jwt"}


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    monkeypatch.setattr(login_script, "login", lambda **_kw: COOKIES)


class TestExitCodes:
    def test_check_mode_verifies_without_publishing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(login_script, "verify_api", lambda _c: (True, "http=200"))
        monkeypatch.setattr(login_script, "publish", lambda _c: calls.append("publish"))

        assert login_script.main(["--check"]) == 0
        assert calls == []

    def test_publish_mode_writes_redis(self, monkeypatch):
        monkeypatch.setattr(login_script, "verify_api", lambda _c: (True, "http=200"))
        published = []
        monkeypatch.setattr(login_script, "publish",
                            lambda cookies: published.append(cookies) or "2026-11-29T07:15")

        assert login_script.main([]) == 0
        assert published == [COOKIES]

    def test_login_failure_returns_2(self, monkeypatch):
        def boom(**_kw):
            raise RuntimeError("kite 503")

        monkeypatch.setattr(login_script, "login", boom)
        assert login_script.main(["--check"]) == 2

    def test_api_rejection_returns_3(self, monkeypatch):
        monkeypatch.setattr(login_script, "verify_api", lambda _c: (False, "http=401"))
        published = []
        monkeypatch.setattr(login_script, "publish", lambda _c: published.append(1))

        assert login_script.main([]) == 3
        assert published == []


class TestVerifyApi:
    def test_200_success_payload_is_accepted(self, monkeypatch):
        import requests

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"success": True}

        monkeypatch.setattr(requests, "get", lambda *a, **k: Response())
        assert login_script.verify_api(COOKIES) == (True, "http=200")

    @pytest.mark.parametrize("status,payload", [(200, {"success": False}), (401, {})])
    def test_rejections(self, monkeypatch, status, payload):
        import requests

        class Response:
            status_code = status

            @staticmethod
            def json():
                return payload

        monkeypatch.setattr(requests, "get", lambda *a, **k: Response())
        ok, detail = login_script.verify_api(COOKIES)
        assert ok is False and f"http={status}" in detail
