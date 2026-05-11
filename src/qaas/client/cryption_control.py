import os
import sys
import base64
import hashlib
import secrets
import string
from cryptography.fernet import Fernet
import logging


# -----------
# Set Logging
# -----------

log = logging.getLoggerClass()(
    __name__, os.environ.get("QPROVIDER_LOGLEVEL", "INFO").upper()
)

# Formatter for consistent output
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

# Decide handler: file or stderr
logfile = os.environ.get("QPROVIDER_LOGFILE")
if logfile:
    handler = logging.FileHandler(logfile, mode="a")
else:
    handler = logging.StreamHandler(sys.stderr)

handler.setFormatter(formatter)
log.addHandler(handler)


def generate_password(length=50):
    """Generate a random 50-character password."""
    raw_password = "".join(
        secrets.choice(string.ascii_letters + string.digits + string.punctuation)
        for _ in range(length)
    )
    p = base64.b64encode(raw_password.encode()).decode()
    # log.debug("Token PWD: %s, %s", raw_password, str(p))
    return raw_password, p


def derive_key(password):
    """Derive a 32-byte Fernet key from a password using SHA-256."""
    # Hash with SHA-256 to get exactly 32 bytes, then encode as base64
    key_bytes = hashlib.sha256(password.encode()).digest()
    return base64.urlsafe_b64encode(key_bytes)


def encrypt_string(plaintext, password):
    """Encrypt a string with a password."""
    key = derive_key(password)
    cipher = Fernet(key)
    encrypted_bytes = cipher.encrypt(plaintext.encode())
    return base64.b64encode(encrypted_bytes).decode()


def decrypt_string(ciphertext: str, password):
    """Decrypt a string with a password."""
    key = derive_key(password)
    cipher = Fernet(key)
    plaintext = cipher.decrypt(base64.b64decode(ciphertext.encode())).decode()
    return plaintext
