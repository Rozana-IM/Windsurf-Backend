# auth.py
from google.oauth2 import id_token
from google.auth.transport import requests as grequests
from fastapi import HTTPException
import logging

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = None  # Optionally set from env or .env

def verify_google_token(id_token_str: str, client_id: str = None):
    """
    Verifies the Google ID token. Returns payload dict if valid.
    """
    cid = client_id or GOOGLE_CLIENT_ID
    try:
        logger.info(f"🔵 Verifying Google token (length: {len(id_token_str)})")
        logger.info(f"🔑 Expected client ID: {cid}")
        
        # If client_id not provided, verification will still validate token but
        # will not check aud (audience). It's recommended to set GOOGLE_CLIENT_ID.
        request = grequests.Request()
        info = id_token.verify_oauth2_token(id_token_str, request, audience=cid)
        
        logger.info(f"✅ Token verified successfully!")
        logger.info(f"📧 Email: {info.get('email')}")
        logger.info(f"👤 Name: {info.get('name')}")
        
        # info includes: email, email_verified, name, picture, sub (user id)
        return info
    except Exception as e:
        logger.error(f"❌ Token verification failed: {type(e).__name__}: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Invalid Google token: {e}")
