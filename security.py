"""
JWT security and token management
Handles authentication tokens to prevent ID usurpation
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
import time
import jwt
from passlib.context import CryptContext
import os
from dotenv import load_dotenv

load_dotenv()

# Secret key for JWT signing - required, no insecure fallback.
# Generate with: openssl rand -hex 32
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY environment variable is not set. "
        "Generate one with `openssl rand -hex 32` and set it before starting the app."
    )
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30  # Token validity period

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create JWT token with user data (primarily eseo_id)

    Args:
        data: Dictionary containing user information (must include 'eseo_id')
        expires_delta: Optional custom expiration time

    Returns:
        Encoded JWT token as string

    Example:
        token = create_access_token({"eseo_id": 54024})
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)

    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

    return encoded_jwt


def verify_token(token: str) -> Optional[dict]:
    """
    Verify JWT token signature and extract payload

    Args:
        token: JWT token string

    Returns:
        Decoded payload if valid, None if invalid/expired

    Example:
        payload = verify_token(token)
        if payload:
            eseo_id = payload.get("eseo_id")
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        # Token has expired
        return None
    except jwt.InvalidTokenError:
        # Token signature is invalid (modified)
        return None


def get_eseo_id_from_token(token: str) -> Optional[int]:
    """
    Extract eseo_id from token (convenience function)

    Args:
        token: JWT token string

    Returns:
        eseo_id as integer if valid, None otherwise
    """
    payload = verify_token(token)
    if payload:
        return payload.get("eseo_id")
    return None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against a hash
    Note: Currently not used as we don't store passwords
    Kept for potential future use
    """
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """
    Hash a password
    Note: Currently not used as we don't store passwords
    Kept for potential future use
    """
    return pwd_context.hash(password)


class RateLimiter:
    """
    Simple in-memory sliding-window rate limiter.

    Suitable for this app's single-worker deployment (required anyway for
    APScheduler compatibility, see Dockerfile). Not shared across processes -
    if the app is ever scaled to multiple workers/instances, replace with a
    shared store (e.g. Redis).
    """

    def __init__(self, max_attempts: int, window_seconds: float):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        """
        Record an attempt for `key` and return True if it's allowed,
        False if the caller has exceeded max_attempts within the window.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds
        attempts = [t for t in self._attempts.get(key, []) if t >= cutoff]

        if len(attempts) >= self.max_attempts:
            self._attempts[key] = attempts
            return False

        attempts.append(now)
        self._attempts[key] = attempts
        return True
