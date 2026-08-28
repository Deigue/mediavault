"""
webguard.py - who is allowed to post to the dashboard.
"""

import hmac
import ipaddress
import os
import secrets

from flask import Response, jsonify, request

CSRF_HEADER = "X-MediaVault-Token"

# Minted per process, so a restart invalidates every open tab's copy. A stale
# tab then gets one clear error telling it to reload, rather than a write that
# quietly does nothing.
CSRF_TOKEN = secrets.token_urlsafe(32)

PASSWORD = os.environ.get("MEDIAVAULT_PASSWORD", "")

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def is_loopback(host):
    """True for an address only this machine can reach."""
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host):
    """
    Refuse to start on an address other machines can reach with no password.

    Every write endpoint here deletes files or rewrites the index, and there
    is no login. Binding wider than localhost publishes that to the network,
    which is a startup error rather than something to warn about and carry on.
    """
    if is_loopback(host) or PASSWORD:
        return
    raise SystemExit(
        f"Refusing to start on {host}.\n"
        f"Other machines can reach that address, and this dashboard can delete\n"
        f"files. Either leave MEDIAVAULT_HOST unset to stay on 127.0.0.1, or\n"
        f"set MEDIAVAULT_PASSWORD to require a password.\n"
        f"A password over plain HTTP is sent in the clear, so it is worth only\n"
        f"as much as the network it crosses."
    )


def _password_ok():
    auth = request.authorization
    return bool(auth and auth.password and hmac.compare_digest(auth.password, PASSWORD))


def _token_ok():
    return hmac.compare_digest(request.headers.get(CSRF_HEADER, ""), CSRF_TOKEN)


def guard():
    """
    Checked before every request. Returns a response to refuse with, or None
    to let it through. Reads are open; only writes need the token.
    """
    if PASSWORD and not _password_ok():
        return Response(
            "MediaVault needs a password.",
            401,
            {"WWW-Authenticate": 'Basic realm="MediaVault"'},
        )
    if request.method in WRITE_METHODS and not _token_ok():
        return jsonify(
            {
                "ok": False,
                "error": "This page's security token is missing or out of date. "
                "Reload the dashboard and try again.",
            }
        ), 403
    return None
