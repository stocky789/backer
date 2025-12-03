"""Secure password handling with Fernet encryption.

This module provides encryption for sensitive data like repository passwords.
The encryption key is stored in a file with restricted permissions (0600).

Security model:
- Protects passwords at rest in the database
- Key file is only readable by the owner
- Does NOT protect against an attacker with full filesystem access
- For stronger protection, use an external secrets manager or HSM
"""

import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretsManager:
    """Manages encryption/decryption of sensitive data."""

    def __init__(self, data_dir: Path | str | None = None):
        """Initialize the secrets manager.

        Args:
            data_dir: Directory to store the key file. Defaults to ~/.local/share/backer
        """
        if data_dir is None:
            data_dir = Path.home() / ".local" / "share" / "backer"
        elif isinstance(data_dir, str):
            data_dir = Path(data_dir)

        self.data_dir = data_dir
        self.key_file = data_dir / "secret.key"
        self._fernet: Fernet | None = None

    def _ensure_key_exists(self) -> None:
        """Ensure the encryption key file exists, creating it if needed."""
        if self.key_file.exists():
            return

        # Create directory with restricted permissions
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Generate a new key
        key = Fernet.generate_key()

        # Write key file
        with open(self.key_file, "wb") as f:
            f.write(key)

        # Set restrictive permissions (owner read/write only)
        # Skip on Windows where chmod doesn't work the same way
        if sys.platform != "win32":
            self.key_file.chmod(0o600)
            self.data_dir.chmod(0o700)

    def _get_fernet(self) -> Fernet:
        """Get or create the Fernet instance."""
        if self._fernet is None:
            self._ensure_key_exists()

            with open(self.key_file, "rb") as f:
                key = f.read().strip()

            self._fernet = Fernet(key)

        return self._fernet

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string.

        Args:
            plaintext: The string to encrypt

        Returns:
            Base64-encoded encrypted string (safe for database storage)
        """
        if not plaintext:
            return ""

        fernet = self._get_fernet()
        encrypted = fernet.encrypt(plaintext.encode("utf-8"))
        return encrypted.decode("utf-8")

    def decrypt(self, ciphertext: str) -> str | None:
        """Decrypt a string.

        Args:
            ciphertext: The encrypted string from encrypt()

        Returns:
            The original plaintext, or None if decryption fails
        """
        if not ciphertext:
            return None

        try:
            fernet = self._get_fernet()
            decrypted = fernet.decrypt(ciphertext.encode("utf-8"))
            return decrypted.decode("utf-8")
        except InvalidToken:
            # Key mismatch or corrupted data
            return None
        except Exception:
            return None

    def is_encrypted(self, value: str) -> bool:
        """Check if a value appears to be Fernet-encrypted.

        Fernet tokens start with 'gAAAAA' (base64-encoded version byte).
        """
        return value.startswith("gAAAAA") if value else False

    def migrate_base64(self, base64_value: str) -> str | None:
        """Migrate a base64-encoded password to encrypted format.

        Args:
            base64_value: The old base64-encoded password

        Returns:
            The newly encrypted password, or None if migration fails
        """
        import base64

        try:
            # Decode the old base64 value
            plaintext = base64.b64decode(base64_value).decode("utf-8")
            # Re-encrypt with Fernet
            return self.encrypt(plaintext)
        except Exception:
            return None


# Global instance - lazily initialized
_secrets_manager: SecretsManager | None = None


def get_secrets_manager(data_dir: Path | str | None = None) -> SecretsManager:
    """Get the global secrets manager instance.

    Args:
        data_dir: Directory for the key file. If provided and different from
                  the current instance's directory, a new instance is created.

    Returns:
        SecretsManager instance
    """
    global _secrets_manager

    # Normalize data_dir for comparison
    if data_dir is not None:
        data_dir = Path(data_dir) if isinstance(data_dir, str) else data_dir

    # Create new instance if none exists or if data_dir changed
    if _secrets_manager is None:
        _secrets_manager = SecretsManager(data_dir)
    elif data_dir is not None and _secrets_manager.data_dir != data_dir:
        _secrets_manager = SecretsManager(data_dir)

    return _secrets_manager
