import os
import time
import hashlib
from pathlib import Path
from typing import Any
import logging

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel
from dotenv import load_dotenv

# Load root .env for backend auth settings.
load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger("auth")


class AuthUser(BaseModel):
    user_id: str
    email: str | None = None
    is_guest: bool = False


_bearer = HTTPBearer(auto_error=False)
_jwks_cache: dict[str, Any] = {"keys": {}, "expires_at": 0}


def _get_issuer() -> str:
    explicit_issuer = os.getenv("SUPABASE_JWT_ISSUER")
    if explicit_issuer:
        return explicit_issuer.rstrip("/")
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not supabase_url:
        raise RuntimeError("SUPABASE_URL or SUPABASE_JWT_ISSUER must be set")
    return f"{supabase_url}/auth/v1"


def _get_audience() -> str:
    return os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")


async def _fetch_jwks() -> dict[str, Any]:
    now = time.time()
    if _jwks_cache["keys"] and now < _jwks_cache["expires_at"]:
        return _jwks_cache["keys"]

    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not supabase_url:
        raise RuntimeError("SUPABASE_URL is required when SUPABASE_JWT_SECRET is not set")

    jwks_url = f"{supabase_url}/auth/v1/.well-known/jwks.json"
    async with httpx.AsyncClient(timeout=5.0) as client:
        res = await client.get(jwks_url)
        res.raise_for_status()
        keys = res.json()

    _jwks_cache["keys"] = keys
    _jwks_cache["expires_at"] = now + 60 * 10
    return keys


async def _decode_token(token: str) -> dict[str, Any]:
    audience = _get_audience()
    issuer = _get_issuer()
    jwt_secret = (os.getenv("SUPABASE_JWT_SECRET") or "").strip().strip('"').strip("'")
    header = jwt.get_unverified_header(token)
    alg = (header.get("alg") or "").upper()
    kid = header.get("kid")
    decode_errors: list[str] = []
    if not alg:
        raise JWTError("Token header missing alg")

    def _decode_with_fallback(key: Any, algorithms: list[str]) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=audience,
                issuer=issuer,
            )
        except Exception:
            # Some Supabase tokens may not carry the expected audience claim.
            return jwt.decode(
                token,
                key,
                algorithms=algorithms,
                issuer=issuer,
                options={"verify_aud": False},
            )

    # Try shared-secret verification for HS* tokens.
    if jwt_secret and alg.startswith("HS"):
        try:
            return _decode_with_fallback(jwt_secret, [alg])
        except Exception as exc:
            decode_errors.append(f"{alg} decode failed: {exc}")

    # Try JWKS verification for asymmetric tokens (RS*/ES*/EdDSA).
    if kid:
        try:
            jwks = await _fetch_jwks()
            key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
            if not key:
                raise JWTError("Signing key not found")
            return _decode_with_fallback(key, [alg])
        except Exception as exc:
            decode_errors.append(f"JWKS decode failed ({alg}): {exc}")
    elif not alg.startswith("HS"):
        decode_errors.append(f"Token header missing kid for asymmetric token ({alg})")

    if decode_errors:
        raise JWTError("; ".join(decode_errors))
    raise JWTError("Unable to decode token")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser:
    if not credentials:
        logger.warning("[AUTH] Missing Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    if credentials.scheme.lower() != "bearer":
        logger.warning("[AUTH] Invalid auth scheme: %s", credentials.scheme)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme",
        )

    token = credentials.credentials
    token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    logger.info("[AUTH] Bearer token received (fp=%s)", token_fingerprint)

    return await verify_access_token(token)


async def verify_access_token(token: str) -> AuthUser:
    token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    try:
        payload = await _decode_token(token)
    except Exception as exc:
        logger.warning("[AUTH] Token decode failed (fp=%s): %s", token_fingerprint, str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = payload.get("sub")
    if not user_id:
        logger.warning("[AUTH] Token missing subject (fp=%s)", token_fingerprint)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject",
        )

    is_guest = isinstance(user_id, str) and user_id.startswith("guest:")
    logger.info("[AUTH] Authenticated user=%s guest=%s (fp=%s)", user_id, is_guest, token_fingerprint)
    return AuthUser(user_id=user_id, email=payload.get("email"), is_guest=is_guest)


def guest_session_key(user_id: str) -> str | None:
    if not isinstance(user_id, str) or not user_id.startswith("guest:"):
        return None
    return user_id.removeprefix("guest:")


def mint_guest_access_token(guest_session_id: str) -> str:
    """Issue HS256 JWT compatible with verify_access_token (needs SUPABASE_JWT_SECRET)."""
    jwt_secret = (os.getenv("SUPABASE_JWT_SECRET") or "").strip().strip('"').strip("'")
    if not jwt_secret:
        raise RuntimeError("SUPABASE_JWT_SECRET is required to mint guest tokens")

    now = int(time.time())
    exp = now + 60 * 60 * 24 * 30  # 30 days
    sub = f"guest:{guest_session_id}"
    payload: dict[str, Any] = {
        "sub": sub,
        "role": "authenticated",
        "iat": now,
        "exp": exp,
        "aud": _get_audience(),
        "iss": _get_issuer(),
    }
    return jwt.encode(payload, jwt_secret, algorithm="HS256")
