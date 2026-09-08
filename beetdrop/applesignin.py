"""Apple ID sign-in, so Beetdrop can mint its own media-user-token.

Reproduces the browser sign-in flow the Apple TV web app uses
(tv.apple.com -> idmsa.apple.com/appleauth/auth):

  1. GET  /authorize/signin      seeds scnt, X-Apple-Auth-Attributes, the
                                  X-Apple-HC hashcash challenge, a session id
  2. POST /signin/init           SRP-6a start (sends A, gets salt/B/challenge)
  3. POST /signin/complete       SRP proof + X-Apple-HC stamp; 409 => 2FA
  4. 2FA  /verify/trusteddevice  or /verify/phone (SMS) security code
  5. GET  /2sv/trust             trusts the session
  6. POST auth.tv.apple.com/auth/v1/web  -> the media-user-token cookie

Ported from the author's Kodi Apple TV addon, where this flow is already
proven against Apple. Apple documents none of it and may change it.

On secrets: the Apple ID password is NEVER written to disk and never
leaves this process - SRP only ever transmits the public ephemeral A and
the client proof M1, so the password itself is not sent to Apple either.
What persists is the session cookies (0600, in the config volume), which
is what lets the token be re-minted later without another sign-in.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

from .srp import SRPClient

AUTH_BASE = "https://idmsa.apple.com/appleauth/auth"
MEDIA_AUTH_URL = "https://auth.tv.apple.com/auth/v1/web"
# The real Apple TV web OAuth client id, as captured from tv.apple.com.
CLIENT_ID = "06f8d74b71c73757a2f82158d5e948ae7bae11ec45fda9a58690f55e35945c51"
REDIRECT_URI = "https://tv.apple.com"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 "
              "Firefox/128.0")
SESSION_FILE = "apple_session.json"
TIMEOUT = 30

STATUS_OK = "ok"
STATUS_NEEDS_2FA = "needs_2fa"
STATUS_ERROR = "error"


# -- hashcash ------------------------------------------------------------
# Apple wants an X-Apple-HC stamp on /signin/complete:
#   1:<bits>:<timestamp>:<challenge>::<counter>
# brute-forced so SHA1(stamp) starts with <bits> zero bits. bits is 10-12,
# so this is a few thousand hashes.


def _leading_zero_bits(data: bytes) -> int:
    count = 0
    for byte in data:
        if byte == 0:
            count += 8
            continue
        mask = 0x80
        while mask:
            if byte & mask:
                return count
            count += 1
            mask >>= 1
        break
    return count


def make_stamp(bits, challenge, timestamp=None, max_iterations=5_000_000) -> str:
    bits = int(bits)
    if timestamp is None:
        timestamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    prefix = "1:%d:%s:%s::" % (bits, timestamp, challenge)
    counter = 0
    while counter < max_iterations:
        candidate = prefix + str(counter)
        if _leading_zero_bits(hashlib.sha1(candidate.encode("utf-8")).digest()) >= bits:
            return candidate
        counter += 1
    return prefix + "0"


def _frame_id() -> str:
    return "auth-" + uuid.uuid4().hex[:16]


def _fd_client_info() -> str:
    # Fraud-detection blob. Apple's obfuscated JS fills "F" with a device
    # fingerprint; the SRP web flow is accepted with an empty F.
    return json.dumps({"U": USER_AGENT, "L": "en_US", "Z": "GMT+00:00",
                       "V": "1.1", "F": ""})


class AppleSignIn:
    """One sign-in attempt, including its 2FA step.

    Held in memory between the sign-in and verify requests; the password is
    used inside login() and then dropped.
    """

    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = Path(config_dir) if config_dir else None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://idmsa.apple.com",
            "Referer": "https://idmsa.apple.com/",
            "X-Requested-With": "XMLHttpRequest",
        })
        self._scnt = None
        self._session_id = None
        self._auth_attributes = None
        self._hc_bits = None
        self._hc_challenge = None
        self._frame = None
        self._phone_id = None
        self.error = ""

    # -- headers ---------------------------------------------------------

    def _oauth_headers(self, extra=None) -> dict:
        frame = self._frame or _frame_id()
        headers = {
            "X-Apple-Widget-Key": CLIENT_ID,
            "X-Apple-OAuth-Client-Id": CLIENT_ID,
            "X-Apple-OAuth-Client-Type": "firstPartyAuth",
            "X-Apple-OAuth-Redirect-URI": REDIRECT_URI,
            "X-Apple-OAuth-Response-Type": "code",
            "X-Apple-OAuth-Response-Mode": "web_message",
            "X-Apple-OAuth-State": frame,
            "X-Apple-Frame-Id": frame,
            "X-Apple-Auth-Context": "tv",
            "X-Apple-Domain-Id": "2",
            "X-Apple-Locale": "en_US",
            "X-Apple-I-FD-Client-Info": _fd_client_info(),
        }
        if self._scnt:
            headers["scnt"] = self._scnt
        if self._session_id:
            headers["X-Apple-ID-Session-Id"] = self._session_id
        if self._auth_attributes:
            headers["X-Apple-Auth-Attributes"] = self._auth_attributes
        if extra:
            headers.update(extra)
        return headers

    def _capture(self, response) -> None:
        headers = response.headers
        if headers.get("scnt"):
            self._scnt = headers["scnt"]
        if headers.get("X-Apple-ID-Session-Id"):
            self._session_id = headers["X-Apple-ID-Session-Id"]
        if headers.get("X-Apple-Auth-Attributes"):
            self._auth_attributes = headers["X-Apple-Auth-Attributes"]
        if headers.get("X-Apple-HC-Bits"):
            self._hc_bits = headers["X-Apple-HC-Bits"]
        if headers.get("X-Apple-HC-Challenge"):
            self._hc_challenge = headers["X-Apple-HC-Challenge"]

    def _bootstrap(self) -> None:
        """GET /authorize/signin to seed scnt, auth-attributes, hashcash."""
        response = self.session.get(
            AUTH_BASE + "/authorize/signin",
            params={"frame_id": self._frame, "language": "en_us",
                    "skVersion": "7", "iframeId": self._frame,
                    "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
                    "response_type": "code", "response_mode": "web_message",
                    "state": self._frame, "authVersion": "latest"},
            headers={"Accept": "text/html,application/xhtml+xml",
                     "Referer": REDIRECT_URI + "/"},
            timeout=TIMEOUT)
        self._capture(response)

    # -- sign-in ---------------------------------------------------------

    def login(self, account_name: str, password: str) -> str:
        """SRP sign-in. The password is used here and never stored."""
        if not account_name or not password:
            self.error = "Apple ID and password are both required"
            return STATUS_ERROR
        try:
            self.session.cookies.clear()
            self._scnt = self._session_id = self._auth_attributes = None
            self._frame = _frame_id()
            self._bootstrap()

            srp = SRPClient(account_name)
            init = self.session.post(
                AUTH_BASE + "/signin/init",
                data=json.dumps({
                    "a": base64.b64encode(srp.public_a_bytes()).decode("ascii"),
                    "accountName": account_name,
                    "protocols": ["s2k", "s2k_fo"]}),
                headers=self._oauth_headers(), timeout=TIMEOUT)
            self._capture(init)
            if init.status_code != 200:
                self.error = "Apple refused the sign-in start (%s)" % init.status_code
                return STATUS_ERROR
            payload = init.json()

            proof = srp.process_challenge(
                password,
                base64.b64decode(payload["salt"]),
                int(payload["iteration"]),
                payload.get("protocol", "s2k"),
                base64.b64decode(payload["b"]))

            headers = self._oauth_headers()
            if self._hc_bits and self._hc_challenge:
                headers["X-Apple-HC"] = make_stamp(self._hc_bits, self._hc_challenge)

            complete = self.session.post(
                AUTH_BASE + "/signin/complete?isRememberMeEnabled=false",
                data=json.dumps({
                    "accountName": account_name,
                    "rememberMe": False,
                    "m1": base64.b64encode(proof).decode("ascii"),
                    "c": payload["c"],
                    "m2": base64.b64encode(
                        srp.expected_server_proof()).decode("ascii")}),
                headers=headers, timeout=TIMEOUT)
            self._capture(complete)

            if complete.status_code in (200, 302):
                return STATUS_OK
            if complete.status_code == 409:
                return self._begin_2fa()
            if complete.status_code in (401, 403):
                self.error = "Apple rejected the Apple ID or password"
                return STATUS_ERROR
            self.error = "Apple refused sign-in (%s)" % complete.status_code
            return STATUS_ERROR
        except KeyError as exc:
            self.error = "unexpected sign-in response (missing %s)" % exc
            return STATUS_ERROR
        except requests.RequestException as exc:
            self.error = "could not reach Apple: %s" % exc
            return STATUS_ERROR
        except Exception as exc:  # never leak a password through a traceback
            self.error = "sign-in failed: %s" % type(exc).__name__
            return STATUS_ERROR
        finally:
            password = ""  # noqa: F841 - drop the reference promptly

    def _begin_2fa(self) -> str:
        """Work out whether Apple will use a trusted device or SMS."""
        try:
            response = self.session.get(AUTH_BASE, headers=self._oauth_headers(),
                                        timeout=TIMEOUT)
            self._capture(response)
            info = response.json() if response.content else {}
        except Exception:
            info = {}
        phones = info.get("trustedPhoneNumbers") or []
        if not info.get("trustedDeviceCount", 0) and phones:
            # SMS-only account: ask Apple to text the first number.
            self._phone_id = phones[0].get("id", 1)
            try:
                self.session.put(
                    AUTH_BASE + "/verify/phone",
                    data=json.dumps({"phoneNumber": {"id": self._phone_id},
                                     "mode": "sms"}),
                    headers=self._oauth_headers(), timeout=TIMEOUT)
            except requests.RequestException:
                pass
        else:
            self._phone_id = None
        return STATUS_NEEDS_2FA

    def submit_code(self, code: str) -> str:
        """Submit the six-digit code (trusted device or SMS)."""
        if not code:
            self.error = "a verification code is required"
            return STATUS_ERROR
        try:
            if self._phone_id:
                url = AUTH_BASE + "/verify/phone/securitycode"
                body = {"phoneNumber": {"id": self._phone_id},
                        "securityCode": {"code": str(code)}, "mode": "sms"}
            else:
                url = AUTH_BASE + "/verify/trusteddevice/securitycode"
                body = {"securityCode": {"code": str(code)}}
            response = self.session.post(url, data=json.dumps(body),
                                         headers=self._oauth_headers(),
                                         timeout=TIMEOUT)
            self._capture(response)
            if response.status_code not in (200, 204):
                self.error = "Apple rejected the verification code"
                return STATUS_ERROR
            # Trust the session so Apple stops prompting for a while - this is
            # what lets the token be re-minted later without another code.
            try:
                trust = self.session.get(AUTH_BASE + "/2sv/trust",
                                         headers=self._oauth_headers(),
                                         timeout=TIMEOUT)
                self._capture(trust)
            except requests.RequestException:
                pass
            self._phone_id = None
            return STATUS_OK
        except requests.RequestException as exc:
            self.error = "could not reach Apple: %s" % exc
            return STATUS_ERROR

    # -- the payoff ------------------------------------------------------

    def mint_media_user_token(self, developer_token: str) -> Optional[str]:
        """Exchange the signed-in session for a media-user-token.

        After sign-in the session holds the myacinfo cookie; posting to
        auth.tv.apple.com with the web developer token makes Apple set the
        media-user-token cookie, which is the credential lyrics need.
        """
        try:
            self.session.post(
                MEDIA_AUTH_URL,
                data=json.dumps({"webAuthorizationFlowContext": "tv"}),
                headers={"Authorization": "Bearer " + developer_token,
                         "Content-Type": "application/json",
                         "Origin": REDIRECT_URI,
                         "Referer": REDIRECT_URI + "/"},
                timeout=TIMEOUT)
        except requests.RequestException as exc:
            self.error = "could not reach Apple: %s" % exc
            return None
        # Read by iteration: cookies.get raises when a name exists for more
        # than one domain.
        for cookie in self.session.cookies:
            if cookie.name == "media-user-token" and cookie.value:
                return cookie.value
        self.error = "signed in, but Apple did not return a media-user-token"
        return None

    # -- session persistence ---------------------------------------------

    def save_session(self) -> Optional[Path]:
        """Persist the cookies (never the password) so the token can be
        re-minted later. 0600, in the config volume."""
        if self.config_dir is None:
            return None
        self.config_dir.mkdir(parents=True, exist_ok=True)
        path = self.config_dir / SESSION_FILE
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain,
                    "path": c.path} for c in self.session.cookies]
        # Create with restrictive permissions before any content is written.
        handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as out:
            json.dump({"cookies": cookies, "saved_at": int(time.time())}, out)
        os.chmod(path, 0o600)
        return path

    def load_session(self) -> bool:
        if self.config_dir is None:
            return False
        path = self.config_dir / SESSION_FILE
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        for cookie in data.get("cookies") or []:
            if isinstance(cookie, dict) and cookie.get("name"):
                self.session.cookies.set(
                    cookie["name"], cookie.get("value"),
                    domain=cookie.get("domain", ""), path=cookie.get("path", "/"))
        return bool(data.get("cookies"))

    def clear_session(self) -> None:
        self.session.cookies.clear()
        if self.config_dir is None:
            return
        path = self.config_dir / SESSION_FILE
        if path.exists():
            path.unlink()
