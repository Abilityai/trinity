"""
Credential Encryption Service

Provides AES-256-GCM encryption for credential files stored in git.
This replaces the complex Redis-based credential system with simple
encrypted files that can be committed to version control.

Format:
{
    "version": 1,
    "algorithm": "AES-256-GCM",
    "nonce": "<base64>",
    "ciphertext": "<base64>"
}

The plaintext is a JSON object mapping file paths to file contents:
{
    ".env": "API_KEY=xxx\nSECRET=yyy",
    ".mcp.json": "{...}",
    ".config/gcloud/sa.json": "{...}"
}
"""
import os
import json
import base64
import logging
from typing import Dict, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.agent_auth import agent_httpx_client

logger = logging.getLogger(__name__)

# Encryption key environment variable
ENCRYPTION_KEY_ENV = "CREDENTIAL_ENCRYPTION_KEY"

# Default credential files to look for
DEFAULT_CREDENTIAL_FILES = [".env", ".mcp.json"]


class CredentialsFileNotFoundError(ValueError):
    """Raised by ``import_to_agent`` when ``.credentials.enc`` is absent.

    Subclasses ``ValueError`` so existing ``except ValueError`` callers (the
    admin import endpoint at ``routers/credentials.py``) keep working
    unchanged — that path *should* surface a 400 because the operator
    explicitly asked for an import. Distinct type lets the auto-import on
    startup (``inject_assigned_credentials``) treat it as a non-error skip
    rather than logging a misleading "failed" status. (#612)
    """


class CredentialEncryptionService:
    """
    Service for encrypting and decrypting credential files.

    Uses AES-256-GCM for authenticated encryption. The encryption key
    must be provided via the CREDENTIAL_ENCRYPTION_KEY environment variable
    as a 32-byte hex string (64 characters).

    Example key generation:
        python -c "import secrets; print(secrets.token_hex(32))"
    """

    def __init__(self, key: Optional[str] = None):
        """
        Initialize the encryption service.

        Args:
            key: 32-byte encryption key as hex string. If not provided,
                 reads from CREDENTIAL_ENCRYPTION_KEY environment variable.
        """
        self._key = key
        self._aesgcm: Optional[AESGCM] = None

    @property
    def key(self) -> bytes:
        """Get the encryption key, loading from env if needed."""
        if self._key:
            key_hex = self._key
        else:
            key_hex = os.getenv(ENCRYPTION_KEY_ENV)

        if not key_hex:
            raise ValueError(
                f"Encryption key not configured. Set {ENCRYPTION_KEY_ENV} environment variable "
                "with a 32-byte hex string (64 characters). Generate with: "
                "python -c \"import secrets; print(secrets.token_hex(32))\""
            )

        try:
            key_bytes = bytes.fromhex(key_hex)
        except ValueError as e:
            raise ValueError(f"Invalid encryption key format - must be hex string: {e}")

        if len(key_bytes) != 32:
            raise ValueError(
                f"Encryption key must be 32 bytes (64 hex chars), got {len(key_bytes)} bytes"
            )

        return key_bytes

    @property
    def aesgcm(self) -> AESGCM:
        """Get or create the AESGCM cipher instance."""
        if self._aesgcm is None:
            self._aesgcm = AESGCM(self.key)
        return self._aesgcm

    def encrypt(self, files: Dict[str, str]) -> str:
        """
        Encrypt credential files to a JSON string.

        Args:
            files: Dict mapping file paths to file contents
                   e.g., {".env": "KEY=value", ".mcp.json": "{}"}

        Returns:
            JSON string containing encrypted data with metadata
        """
        # Serialize files to JSON
        plaintext = json.dumps(files, ensure_ascii=False).encode('utf-8')

        # Generate random 12-byte nonce (recommended for GCM)
        nonce = os.urandom(12)

        # Encrypt with authenticated encryption
        ciphertext = self.aesgcm.encrypt(nonce, plaintext, None)

        # Package as JSON
        encrypted_data = {
            "version": 1,
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode('ascii'),
            "ciphertext": base64.b64encode(ciphertext).decode('ascii')
        }

        return json.dumps(encrypted_data, indent=2)

    def decrypt(self, encrypted: str) -> Dict[str, str]:
        """
        Decrypt credential files from a JSON string.

        Args:
            encrypted: JSON string from encrypt()

        Returns:
            Dict mapping file paths to file contents

        Raises:
            ValueError: If decryption fails or format is invalid
        """
        try:
            data = json.loads(encrypted)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid encrypted data format: {e}")

        # Validate format
        if data.get("version") != 1:
            raise ValueError(f"Unsupported encryption version: {data.get('version')}")

        if data.get("algorithm") != "AES-256-GCM":
            raise ValueError(f"Unsupported algorithm: {data.get('algorithm')}")

        try:
            nonce = base64.b64decode(data["nonce"])
            ciphertext = base64.b64decode(data["ciphertext"])
        except (KeyError, ValueError) as e:
            raise ValueError(f"Invalid encrypted data structure: {e}")

        # Decrypt
        try:
            plaintext = self.aesgcm.decrypt(nonce, ciphertext, None)
        except Exception as e:
            raise ValueError(f"Decryption failed - wrong key or corrupted data: {e}")

        # Parse decrypted JSON
        try:
            files = json.loads(plaintext.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Decrypted data is not valid JSON: {e}")

        return files

    async def read_agent_credential_files(
        self,
        agent_name: str,
        file_paths: Optional[list] = None
    ) -> Dict[str, str]:
        """
        Read credential files from a running agent.

        Args:
            agent_name: Name of the agent
            file_paths: List of file paths to read. Defaults to DEFAULT_CREDENTIAL_FILES.

        Returns:
            Dict mapping file paths to contents (only files that exist)
        """
        if file_paths is None:
            file_paths = DEFAULT_CREDENTIAL_FILES

        files = {}

        async with agent_httpx_client(agent_name) as client:
            response = await client.get(
                f"http://agent-{agent_name}:8000/api/credentials/read",
                params={"paths": ",".join(file_paths)},
                timeout=30.0
            )

            if response.status_code == 200:
                data = response.json()
                files = data.get("files", {})
            else:
                logger.warning(
                    f"Failed to read credentials from agent {agent_name}: "
                    f"{response.status_code}"
                )

        return files

    async def write_agent_credential_files(
        self,
        agent_name: str,
        files: Dict[str, str]
    ) -> Dict[str, any]:
        """
        Write credential files to a running agent.

        Args:
            agent_name: Name of the agent
            files: Dict mapping file paths to contents

        Returns:
            Response from agent with written files list
        """
        async with agent_httpx_client(agent_name) as client:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/credentials/inject",
                json={"files": files},
                timeout=30.0
            )

            if response.status_code != 200:
                error = response.json().get("detail", "Unknown error")
                raise RuntimeError(f"Failed to write credentials to agent: {error}")

            return response.json()

    async def export_to_agent(
        self,
        agent_name: str,
        file_paths: Optional[list] = None
    ) -> str:
        """
        Export credentials from agent to encrypted file in workspace.

        Reads credential files from agent, encrypts them, and writes
        .credentials.enc to the agent's workspace.

        Args:
            agent_name: Name of the agent
            file_paths: List of file paths to export

        Returns:
            Path to the encrypted file written
        """
        # Read credential files from agent
        files = await self.read_agent_credential_files(agent_name, file_paths)

        if not files:
            raise ValueError("No credential files found to export")

        # Encrypt
        encrypted = self.encrypt(files)

        # Write .credentials.enc to agent workspace
        await self.write_agent_credential_files(
            agent_name,
            {".credentials.enc": encrypted}
        )

        logger.info(f"Exported {len(files)} credential files to .credentials.enc for agent {agent_name}")

        return ".credentials.enc"

    async def import_to_agent(self, agent_name: str) -> Dict[str, str]:
        """
        Import credentials from encrypted file to agent.

        Reads .credentials.enc from agent workspace, decrypts, and
        writes the credential files to the workspace.

        Args:
            agent_name: Name of the agent

        Returns:
            Dict of imported files
        """
        # Read encrypted file from agent
        files = await self.read_agent_credential_files(
            agent_name,
            [".credentials.enc"]
        )

        encrypted = files.get(".credentials.enc")
        if not encrypted:
            # #612: distinguish "no encrypted file" from other decode/IO
            # failures so the auto-import startup path can skip silently
            # while the admin-triggered import still surfaces a 400.
            raise CredentialsFileNotFoundError(
                "No .credentials.enc file found in agent workspace"
            )

        # Decrypt
        credential_files = self.decrypt(encrypted)

        if not credential_files:
            raise ValueError("Encrypted file contains no credential files")

        # Write decrypted files to agent
        await self.write_agent_credential_files(agent_name, credential_files)

        logger.info(f"Imported {len(credential_files)} credential files from .credentials.enc for agent {agent_name}")

        return credential_files


# Singleton instance
_service: Optional[CredentialEncryptionService] = None


def get_credential_encryption_service() -> CredentialEncryptionService:
    """Get the credential encryption service singleton."""
    global _service
    if _service is None:
        _service = CredentialEncryptionService()
    return _service
