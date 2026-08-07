import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import select
from app.models import ExchangeAccount


def credential_cipher(settings):
    key = getattr(settings, "credential_encryption_key", "")
    if not key or len(key) < 32:
        raise HTTPException(
            503,
            "Credential encryption is not configured. Set a persistent CREDENTIAL_ENCRYPTION_KEY of at least 32 characters and restart the backend.",
        )
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def load_stored_settings(db, settings):
    row = db.scalar(
        select(ExchangeAccount)
        .where(
            ExchangeAccount.encrypted_api_key.is_not(None),
            ExchangeAccount.environment == "testnet",
        )
        .order_by(ExchangeAccount.id.desc())
    )
    if not row:
        raise HTTPException(404, "No Binance Testnet credentials saved")
    try:
        cipher = credential_cipher(settings)
        api_key = cipher.decrypt(row.encrypted_api_key.encode()).decode()
        secret = cipher.decrypt(row.encrypted_api_secret.encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(500, "Stored credentials cannot be decrypted") from exc
    return row, settings.model_copy(
        update={
            "binance_environment": row.environment,
            "binance_api_key": api_key,
            "binance_api_secret": secret,
        }
    )
