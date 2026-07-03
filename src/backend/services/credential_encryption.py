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

# Encryption key environment variable (primary — used for ALL new writes)
ENCRYPTION_KEY_ENV = "CREDENTIAL_ENCRYPTION_KEY"

# Optional secondary key (#267) — DECRYPT-ONLY fallback used during key rotation.
# Set it to the *previous* key while CREDENTIAL_ENCRYPTION_KEY holds the new one:
# existing ciphertext still decrypts (via the secondary), new writes use the
# primary, and `scripts/deploy/rotate-credential-key.py` re-encrypts persisted
# secrets onto the primary. Once the sweep completes the secondary can be removed.
SECONDARY_ENCRYPTION_KEY_ENV = "CREDENTIAL_ENCRYPTION_KEY_SECONDARY"

# Default credential files to look for
DEFAULT_CREDENTIAL_FILES = [".env", ".mcp.json"]

# Marker on the inner JSON of a v2 (binary-capable) `.credentials.enc` archive
# (#11). Absent ⇒ legacy flat `{path: text}` archive.
_ARCHIVE_V2_MARKER = "__cred_archive_v2__"


def validate_credential_set(files: Dict[str, str], files_b64: Dict[str, str] = None) -> None:
    """Enforce the curated credential policy + `.mcp.json` content guard over a
    text/binary credential set (#11 review). Raises ``ValueError`` on violation.

    Used on the IMPORT boundary so the backend re-validates a decrypted archive
    before writing — the dual-layer (backend + agent-server) the issue mandates,
    rather than trusting the downstream /inject check alone. The user-facing
    inject router enforces the same rules inline (HTTP 400)."""
    from services.credential_paths import disallowed_paths
    files_b64 = files_b64 or {}
    bad = disallowed_paths(list(files) + list(files_b64))
    if bad:
        raise ValueError(f"Archive contains disallowed credential path(s): {bad}")
    if ".mcp.json" in files_b64:
        raise ValueError(".mcp.json may not be binary (content must be validatable)")
    if ".mcp.json" in files:
        from services.mcp_validator import validate_mcp_config, McpValidationError
        try:
            validate_mcp_config(files[".mcp.json"])
        except McpValidationError as e:
            raise ValueError(f"Invalid .mcp.json in archive: {e}")


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
        self._aesgcm_secondary: Optional[AESGCM] = None  # #267 rotation fallback

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

    @property
    def aesgcm_secondary(self) -> Optional[AESGCM]:
        """Decrypt-only cipher for the previous key during rotation (#267).

        Returns None when ``CREDENTIAL_ENCRYPTION_KEY_SECONDARY`` is unset, so a
        single-key deployment behaves exactly as before. An invalid secondary is
        logged and ignored (it must never break the primary decrypt path)."""
        if self._aesgcm_secondary is not None:
            return self._aesgcm_secondary
        key_hex = os.getenv(SECONDARY_ENCRYPTION_KEY_ENV)
        if not key_hex:
            return None
        try:
            key_bytes = bytes.fromhex(key_hex)
            if len(key_bytes) != 32:
                raise ValueError(f"must be 32 bytes (64 hex chars), got {len(key_bytes)}")
        except ValueError as e:
            logger.warning(
                "Ignoring invalid %s (rotation decrypt-fallback disabled): %s",
                SECONDARY_ENCRYPTION_KEY_ENV, e,
            )
            return None
        self._aesgcm_secondary = AESGCM(key_bytes)
        return self._aesgcm_secondary

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

        # Decrypt with the primary key; during rotation (#267) fall back to the
        # decrypt-only secondary key so ciphertext written under the previous key
        # still opens. GCM's auth tag makes a wrong key a clean failure.
        try:
            plaintext = self.aesgcm.decrypt(nonce, ciphertext, None)
        except Exception as primary_err:
            secondary = self.aesgcm_secondary
            if secondary is None:
                raise ValueError(f"Decryption failed - wrong key or corrupted data: {primary_err}")
            try:
                plaintext = secondary.decrypt(nonce, ciphertext, None)
            except Exception:
                raise ValueError(f"Decryption failed - wrong key or corrupted data: {primary_err}")

        # Parse decrypted JSON
        try:
            files = json.loads(plaintext.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Decrypted data is not valid JSON: {e}")

        return files

    # ------------------------------------------------------------------
    # Binary-aware archive (#11). `encrypt`/`decrypt` above stay flat
    # `{path: text}` (back-compat: SIEM/2FA/SSO single-secret callers + legacy
    # `.credentials.enc` files). These wrap a v2 inner structure carrying both
    # text and base64 binary, reusing the SAME AES-GCM envelope.
    # ------------------------------------------------------------------

    def encrypt_files(self, files: Dict[str, str], files_b64: Dict[str, str] = None) -> str:
        """Encrypt a text+binary credential set into a v2 `.credentials.enc`."""
        inner = {_ARCHIVE_V2_MARKER: 1, "files": files or {}, "files_b64": files_b64 or {}}
        return self.encrypt(inner)

    def decrypt_files(self, encrypted: str):
        """Return ``(files, files_b64)``. Reads v2 archives AND legacy flat
        `{path: text}` archives (which carry no binary)."""
        data = self.decrypt(encrypted)
        if isinstance(data, dict) and data.get(_ARCHIVE_V2_MARKER):
            return data.get("files", {}), data.get("files_b64", {})
        return data, {}

    def rewrap(self, encrypted: str) -> str:
        """Re-encrypt an existing envelope under the PRIMARY key (#267).

        Decrypts with whichever key works (primary or rotation secondary) and
        re-encrypts with the primary. Works uniformly for single-secret DB
        tokens and v2 `.credentials.enc` archives — both ride the same envelope
        through ``encrypt``/``decrypt``. Idempotent: an envelope already on the
        primary key round-trips to an equivalent envelope (fresh nonce). The
        re-encryption sweep (`scripts/deploy/rotate-credential-key.py`) calls
        this per persisted secret to migrate the fleet onto a rotated key."""
        return self.encrypt(self.decrypt(encrypted))

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

    async def read_agent_credential_files_split(self, agent_name: str, file_paths: list):
        """Read ``file_paths`` from the agent, returning ``(files, files_b64)`` —
        text in ``files``, non-UTF-8 (binary) creds base64 in ``files_b64`` (#11)."""
        async with agent_httpx_client(agent_name) as client:
            response = await client.get(
                f"http://agent-{agent_name}:8000/api/credentials/read",
                params={"paths": ",".join(file_paths)},
                timeout=30.0,
            )
            if response.status_code != 200:
                logger.warning(f"Failed to read credentials from agent {agent_name}: {response.status_code}")
                return {}, {}
            data = response.json()
            return data.get("files", {}), data.get("files_b64", {})

    async def list_agent_credential_files(self, agent_name: str):
        """Return the list of allow-policy credential paths present in the agent
        (via the agent `/api/credentials/list`). Empty list on older images that
        lack the endpoint, so callers fall back to DEFAULT_CREDENTIAL_FILES (#11)."""
        try:
            async with agent_httpx_client(agent_name) as client:
                response = await client.get(
                    f"http://agent-{agent_name}:8000/api/credentials/list",
                    timeout=30.0,
                )
                if response.status_code != 200:
                    return []
                return [item["path"] for item in response.json().get("files", [])]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"credentials/list unavailable for {agent_name}: {e}")
            return []

    async def write_agent_credential_files(
        self,
        agent_name: str,
        files: Dict[str, str],
        files_b64: Dict[str, str] = None,
    ) -> Dict[str, any]:
        """
        Write credential files to a running agent.

        Args:
            agent_name: Name of the agent
            files: Dict mapping file paths to text contents
            files_b64: Dict mapping file paths to base64 binary contents (#11)

        Returns:
            Response from agent with written files list
        """
        async with agent_httpx_client(agent_name) as client:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/credentials/inject",
                json={"files": files, "files_b64": files_b64 or {}},
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
            (path_to_encrypted_file, number_of_files_captured)
        """
        # Discover the full injected credential set (#11). When the caller
        # doesn't pin paths, ask the agent which allow-policy files exist; fall
        # back to the legacy 2-file default on older images without /list.
        if file_paths is None:
            discovered = await self.list_agent_credential_files(agent_name)
            file_paths = discovered or DEFAULT_CREDENTIAL_FILES
        # Never fold a prior archive into the new one.
        file_paths = [p for p in file_paths if p != ".credentials.enc"]

        # Read split into text + binary
        files, files_b64 = await self.read_agent_credential_files_split(agent_name, file_paths)

        if not files and not files_b64:
            raise ValueError("No credential files found to export")

        # Encrypt the full set (binary-capable v2 archive)
        encrypted = self.encrypt_files(files, files_b64)

        # Write .credentials.enc to agent workspace
        await self.write_agent_credential_files(
            agent_name,
            {".credentials.enc": encrypted}
        )

        total = len(files) + len(files_b64)
        logger.info(f"Exported {total} credential files to .credentials.enc for agent {agent_name}")

        return ".credentials.enc", total

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

        # Decrypt (handles v2 binary archives AND legacy flat archives)
        files, files_b64 = self.decrypt_files(encrypted)

        if not files and not files_b64:
            raise ValueError("Encrypted file contains no credential files")

        # #11 review: enforce the curated policy + .mcp.json content guard on the
        # IMPORT boundary too (backend layer), not only the agent-server /inject
        # layer. An archive can only be forged with the server key, but dual-layer
        # is the issue's invariant — don't rely solely on the downstream check.
        validate_credential_set(files, files_b64)

        # Write decrypted files to agent (text + binary)
        await self.write_agent_credential_files(agent_name, files, files_b64)

        total = len(files) + len(files_b64)
        logger.info(f"Imported {total} credential files from .credentials.enc for agent {agent_name}")

        return {**files, **files_b64}


# Singleton instance
_service: Optional[CredentialEncryptionService] = None


def get_credential_encryption_service() -> CredentialEncryptionService:
    """Get the credential encryption service singleton."""
    global _service
    if _service is None:
        _service = CredentialEncryptionService()
    return _service
