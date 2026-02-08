"""
JWT security and token management
Handles authentication tokens to prevent ID usurpation
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
import jwt
from passlib.context import CryptContext
import os

# Secret key for JWT signing - MUST be changed in production
# Generate with: openssl rand -hex 32
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "eb36532d36a7c825a249d3aaef288a3bbc762660563bb5946ba40150a756af26")
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
