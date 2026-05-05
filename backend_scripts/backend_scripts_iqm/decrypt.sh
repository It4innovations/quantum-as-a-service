#!/bin/bash

# Read the Base64-encoded password from the first argument
B64_PASSWORD="$1"

# Read encrypted data from stdin
read -r ENCRYPTED_DATA

# Pass the data into the inline Python script
python3 -c "
import sys, base64, hashlib
from cryptography.fernet import Fernet

try:
    # 1. Decode the Base64 password back to plain text
    encoded_pwd = sys.argv[1]
    password = base64.b64decode(encoded_pwd).decode('utf-8')
    
    encrypted_data = sys.stdin.read().strip()

    # 2. Derive the key using SHA-256
    key_bytes = hashlib.sha256(password.encode('utf-8')).digest()
    key = base64.urlsafe_b64encode(key_bytes)
    
    # 3. Strip the extra Base64 layer from the token, then Decrypt
    cipher = Fernet(key)
    raw_fernet_token = base64.b64decode(encrypted_data.encode('utf-8'))
    decrypted = cipher.decrypt(raw_fernet_token).decode('utf-8')
    
    print(decrypted)
except base64.binascii.Error:
    print('Decryption failed: Password or Token is not valid Base64.', file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f'Decryption failed: Incorrect password or corrupted data. ({e})', file=sys.stderr)
    sys.exit(1)
" "$B64_PASSWORD" <<< "$ENCRYPTED_DATA"