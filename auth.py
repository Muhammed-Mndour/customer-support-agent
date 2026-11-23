"""Everything about who is calling the API: config now, then password hashing and JWTs."""
import os
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv
from pwdlib import PasswordHash

# Read .env here too, so this module works whatever is imported first.
load_dotenv(override=True)


def _required(name):
    """A setting the app can't run without: stop at startup instead of running with a bad value."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing from .env (see .env.example)")
    return value


JWT_SECRET = _required("JWT_SECRET")                      # signs every token; never commit it
SUPERVISOR_EMAIL = _required("SUPERVISOR_EMAIL")          # the one supervisor, created at startup
SUPERVISOR_PASSWORD = _required("SUPERVISOR_PASSWORD")


# --- Passwords ----------------------------------------------------------------------
# argon2id with pwdlib's recommended settings. Each hash carries its own random salt,
# so the same password gives a different hash every time.
_password_hasher = PasswordHash.recommended()


def hash_password(password):
    """Turn a password into the hash we store. The password itself is never stored."""
    return _password_hasher.hash(password)


def verify_password(password, password_hash):
    """True if the password matches the stored hash, False otherwise."""
    return _password_hasher.verify(password, password_hash)


# Checked at login when the email doesn't exist, so that case takes as long as a wrong
# password. Without it, a fast reply would tell an attacker the email isn't registered.
DUMMY_PASSWORD_HASH = hash_password("not-a-real-password")


# --- Tokens -------------------------------------------------------------------------
JWT_ALGORITHM = "HS256"        # signed and checked with the same secret
ACCESS_TOKEN_MINUTES = 30      # after this the customer has to log in again


def create_access_token(user_id, role):
    """Made once, at login: a signed card saying who this is and until when it is valid.

    Anyone can read what's inside a JWT, so it holds ids only, never a password.
    Only the server can sign one, because only it knows JWT_SECRET.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),   # the JWT standard wants the subject as a string
        "role": role,
        "iat": now,            # issued at
        "exp": now + timedelta(minutes=ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


class InvalidToken(Exception):
    """The token can't be trusted: forged, edited, expired, or not a token at all."""


def decode_access_token(token):
    """Run on every request: check the card, then say who it belongs to.

    PyJWT checks three things inside jwt.decode:
    - the algorithm is HS256. We choose it here and never trust the token's own header,
      so a token saying "alg": "none" (no signature) is refused.
    - the signature: HMAC-SHA256 of header.payload with JWT_SECRET must match.
    - exp is still in the future.
    Returns {"user_id": int, "role": str}, or raises InvalidToken.
    """
    try:
        payload = jwt.decode(
            token, JWT_SECRET, algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "role", "exp"]},  # a token without them is refused
        )
        return {"user_id": int(payload["sub"]), "role": payload["role"]}
    except (jwt.PyJWTError, ValueError) as error:
        raise InvalidToken(str(error)) from error
