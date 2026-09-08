"""Apple ID sign-in: the SRP flow, 2FA, token minting, session handling,
and - most importantly - that the password is never persisted anywhere.
All HTTP is faked."""

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

import beetdrop.applesignin as signin
from beetdrop.app import create_app
from beetdrop.config import Config
from beetdrop.srp import SRPClient

PASSWORD = "hunter2-should-never-be-written"


class FakeResp:
    def __init__(self, status=200, data=None, headers=None, content=b"{}"):
        self.status_code = status
        self._data = data if data is not None else {}
        self.headers = headers or {}
        self.content = content
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


class FakeCookie:
    def __init__(self, name, value, domain="apple.com", path="/"):
        self.name, self.value, self.domain, self.path = name, value, domain, path


class FakeJar(list):
    def clear(self):
        del self[:]

    def set(self, name, value, domain="", path="/"):
        self.append(FakeCookie(name, value, domain, path))


def init_payload():
    return {"salt": "c2FsdHNhbHQ=", "iteration": 100, "protocol": "s2k",
            "b": "ZGVhZGJlZWZkZWFkYmVlZg==", "c": "challenge-token"}


@pytest.fixture
def flow(tmp_path, monkeypatch):
    f = signin.AppleSignIn(tmp_path / "config")
    f.session.cookies = FakeJar()
    # Hashcash is a real proof-of-work; keep the tests instant.
    monkeypatch.setattr(signin, "make_stamp", lambda *a, **k: "1:10:x::0")
    return f


class TestSrp:
    def test_password_is_never_transmitted(self):
        """SRP sends only the public A and the proof M1."""
        client = SRPClient("me@example.com")
        proof = client.process_challenge(
            PASSWORD, b"saltsalt", 100, "s2k", (0xDEADBEEF).to_bytes(32, "big"))
        assert PASSWORD.encode() not in proof
        assert PASSWORD.encode() not in client.public_a_bytes()
        assert len(proof) == 32

    def test_rejects_bad_server_value(self):
        client = SRPClient("me@example.com")
        with pytest.raises(ValueError):
            client.process_challenge(PASSWORD, b"s", 10, "s2k", b"\x00" * 32)


class TestSignInFlow:
    def test_successful_signin(self, flow, monkeypatch):
        def post(url, **kwargs):
            if "/signin/init" in url:
                return FakeResp(data=init_payload())
            if "/signin/complete" in url:
                return FakeResp(status=200)
            return FakeResp()
        monkeypatch.setattr(flow.session, "get", lambda *a, **k: FakeResp())
        monkeypatch.setattr(flow.session, "post", post)
        assert flow.login("me@example.com", PASSWORD) == signin.STATUS_OK

    def test_409_starts_two_factor(self, flow, monkeypatch):
        def post(url, **kwargs):
            if "/signin/init" in url:
                return FakeResp(data=init_payload())
            return FakeResp(status=409)
        monkeypatch.setattr(flow.session, "get", lambda *a, **k: FakeResp(
            data={"trustedDeviceCount": 1}))
        monkeypatch.setattr(flow.session, "post", post)
        assert flow.login("me@example.com", PASSWORD) == signin.STATUS_NEEDS_2FA

    def test_sms_only_account_requests_a_text(self, flow, monkeypatch):
        asked = {}

        def post(url, **kwargs):
            if "/signin/init" in url:
                return FakeResp(data=init_payload())
            return FakeResp(status=409)
        monkeypatch.setattr(flow.session, "get", lambda *a, **k: FakeResp(
            data={"trustedDeviceCount": 0,
                  "trustedPhoneNumbers": [{"id": 7}]}))
        monkeypatch.setattr(flow.session, "post", post)
        monkeypatch.setattr(flow.session, "put",
                            lambda url, **k: asked.update(url=url) or FakeResp())
        assert flow.login("me@example.com", PASSWORD) == signin.STATUS_NEEDS_2FA
        assert "verify/phone" in asked["url"]
        assert flow._phone_id == 7

    def test_wrong_password_is_reported(self, flow, monkeypatch):
        def post(url, **kwargs):
            if "/signin/init" in url:
                return FakeResp(data=init_payload())
            return FakeResp(status=401)
        monkeypatch.setattr(flow.session, "get", lambda *a, **k: FakeResp())
        monkeypatch.setattr(flow.session, "post", post)
        assert flow.login("me@example.com", "wrong") == signin.STATUS_ERROR
        assert "rejected" in flow.error

    def test_missing_credentials(self, flow):
        assert flow.login("", PASSWORD) == signin.STATUS_ERROR
        assert flow.login("me@example.com", "") == signin.STATUS_ERROR

    def test_code_verification_and_trust(self, flow, monkeypatch):
        seen = []
        monkeypatch.setattr(flow.session, "post",
                            lambda url, **k: seen.append(url) or FakeResp(204))
        monkeypatch.setattr(flow.session, "get",
                            lambda url, **k: seen.append(url) or FakeResp())
        assert flow.submit_code("123456") == signin.STATUS_OK
        assert any("verify/trusteddevice" in u for u in seen)
        assert any("2sv/trust" in u for u in seen)  # so Apple stops prompting

    def test_bad_code_rejected(self, flow, monkeypatch):
        monkeypatch.setattr(flow.session, "post", lambda *a, **k: FakeResp(401))
        assert flow.submit_code("000000") == signin.STATUS_ERROR


class TestTokenMinting:
    def test_mints_from_cookie(self, flow, monkeypatch):
        def post(url, **kwargs):
            flow.session.cookies.append(
                FakeCookie("media-user-token", "minted-token"))
            return FakeResp()
        monkeypatch.setattr(flow.session, "post", post)
        assert flow.mint_media_user_token("dev") == "minted-token"

    def test_no_cookie_is_an_error(self, flow, monkeypatch):
        monkeypatch.setattr(flow.session, "post", lambda *a, **k: FakeResp())
        assert flow.mint_media_user_token("dev") is None
        assert "media-user-token" in flow.error


class TestSessionPersistence:
    def test_session_file_is_owner_only_and_has_no_password(self, flow):
        flow.session.cookies.append(FakeCookie("myacinfo", "cookie-value"))
        path = flow.save_session()
        assert path.is_file()
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, "session file must be owner-only, got %o" % mode
        body = path.read_text()
        assert PASSWORD not in body
        assert "password" not in body.lower()
        assert "cookie-value" in body

    def test_round_trip_and_clear(self, tmp_path, flow):
        flow.session.cookies.append(FakeCookie("myacinfo", "v"))
        flow.save_session()
        other = signin.AppleSignIn(flow.config_dir)
        other.session.cookies = FakeJar()
        assert other.load_session() is True
        assert any(c.name == "myacinfo" for c in other.session.cookies)
        other.clear_session()
        assert not (flow.config_dir / signin.SESSION_FILE).exists()

    def test_load_without_a_file(self, tmp_path):
        flow = signin.AppleSignIn(tmp_path / "nope")
        assert flow.load_session() is False


class TestEndpoints:
    def make_config(self, tmp_path):
        music = tmp_path / "music"
        music.mkdir()
        return Config(music_root=music, scratch_root=tmp_path / "s",
                      config_dir=tmp_path / "c")

    def test_signin_stores_token_without_exposing_it(self, tmp_path, monkeypatch):
        import beetdrop.app as app_module
        config = self.make_config(tmp_path)

        monkeypatch.setattr(app_module.applesignin.AppleSignIn, "login",
                            lambda self, a, p: signin.STATUS_OK)
        monkeypatch.setattr(app_module.applesignin.AppleSignIn,
                            "mint_media_user_token", lambda self, dev: "tok-123")
        monkeypatch.setattr(app_module.applesignin.AppleSignIn,
                            "save_session", lambda self: None)
        monkeypatch.setattr(app_module.apple, "fetch_developer_token",
                            lambda force=False: "dev")

        with TestClient(create_app(config)) as client:
            body = client.post("/api/apple/signin",
                               json={"apple_id": "me@x.com",
                                     "password": PASSWORD}).json()
            assert body["status"] == "ok" and body["token_set"] is True
            settings = client.get("/api/settings").json()
            assert settings["apple_token_set"] is True
            # Neither the token nor the password comes back out.
            assert "tok-123" not in str(settings)
            assert PASSWORD not in str(settings)

    def test_two_factor_round_trip(self, tmp_path, monkeypatch):
        import beetdrop.app as app_module
        config = self.make_config(tmp_path)
        monkeypatch.setattr(app_module.applesignin.AppleSignIn, "login",
                            lambda self, a, p: signin.STATUS_NEEDS_2FA)
        monkeypatch.setattr(app_module.applesignin.AppleSignIn, "submit_code",
                            lambda self, code: signin.STATUS_OK)
        monkeypatch.setattr(app_module.applesignin.AppleSignIn,
                            "mint_media_user_token", lambda self, dev: "tok-2fa")
        monkeypatch.setattr(app_module.applesignin.AppleSignIn,
                            "save_session", lambda self: None)
        monkeypatch.setattr(app_module.apple, "fetch_developer_token",
                            lambda force=False: "dev")

        with TestClient(create_app(config)) as client:
            first = client.post("/api/apple/signin",
                                json={"apple_id": "me@x.com",
                                      "password": PASSWORD}).json()
            assert first["status"] == "needs_2fa" and first["flow_id"]
            second = client.post("/api/apple/verify",
                                 json={"flow_id": first["flow_id"],
                                       "code": "123456"}).json()
            assert second["status"] == "ok"
            assert client.get("/api/settings").json()["apple_token_set"] is True

    def test_unknown_flow_is_gone(self, tmp_path):
        config = self.make_config(tmp_path)
        with TestClient(create_app(config)) as client:
            response = client.post("/api/apple/verify",
                                   json={"flow_id": "nope", "code": "1"})
        assert response.status_code == 410

    def test_failed_signin_is_401(self, tmp_path, monkeypatch):
        import beetdrop.app as app_module
        config = self.make_config(tmp_path)

        def fail(self, a, p):
            self.error = "Apple rejected the Apple ID or password"
            return signin.STATUS_ERROR
        monkeypatch.setattr(app_module.applesignin.AppleSignIn, "login", fail)
        with TestClient(create_app(config)) as client:
            response = client.post("/api/apple/signin",
                                   json={"apple_id": "me@x.com", "password": "x"})
        assert response.status_code == 401
        assert "rejected" in response.json()["detail"]

    def test_signout_clears_token(self, tmp_path, monkeypatch):
        import beetdrop.app as app_module
        config = self.make_config(tmp_path)
        monkeypatch.setattr(app_module.applesignin.AppleSignIn,
                            "clear_session", lambda self: None)
        with TestClient(create_app(config)) as client:
            client.put("/api/settings", json={"apple_token": "existing"})
            assert client.get("/api/settings").json()["apple_token_set"] is True
            client.post("/api/apple/signout")
            assert client.get("/api/settings").json()["apple_token_set"] is False


class TestPasswordIsNeverPersisted:
    """The whole point of the design: nothing on disk ever holds it."""

    def test_nothing_written_anywhere_contains_the_password(
            self, tmp_path, monkeypatch):
        import beetdrop.app as app_module
        music = tmp_path / "music"
        music.mkdir()
        config = Config(music_root=music, scratch_root=tmp_path / "s",
                        config_dir=tmp_path / "c")

        # Real save_session runs; only the network is faked.
        monkeypatch.setattr(app_module.applesignin.AppleSignIn, "login",
                            lambda self, a, p: signin.STATUS_OK)
        monkeypatch.setattr(app_module.applesignin.AppleSignIn,
                            "mint_media_user_token", lambda self, dev: "tok")
        monkeypatch.setattr(app_module.apple, "fetch_developer_token",
                            lambda force=False: "dev")

        with TestClient(create_app(config)) as client:
            client.post("/api/apple/signin",
                        json={"apple_id": "me@x.com", "password": PASSWORD})

        offenders = []
        for path in tmp_path.rglob("*"):
            if path.is_file():
                try:
                    if PASSWORD in path.read_bytes().decode("utf-8", "ignore"):
                        offenders.append(str(path))
                except OSError:
                    pass
        assert offenders == [], "password leaked into %s" % offenders
