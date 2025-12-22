from typing import Dict, List, Optional
from pydantic import BaseModel
from datetime import datetime
import json
import redis
import secrets
import httpx
from urllib.parse import urlencode

class CredentialType(str):
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    BASIC_AUTH = "basic_auth"
    TOKEN = "token"


# MCP API Key models have been moved to database.py for SQLite persistence


class OAuthProvider(str):
    GOOGLE = "google"
    SLACK = "slack"
    GITHUB = "github"
    NOTION = "notion"

class CredentialCreate(BaseModel):
    name: str
    service: str
    type: str
    credentials: Dict
    description: Optional[str] = None

class CredentialUpdate(BaseModel):
    name: Optional[str] = None
    credentials: Optional[Dict] = None
    description: Optional[str] = None

class Credential(BaseModel):
    id: str
    name: str
    service: str
    type: str
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    status: str = "active"

class OAuthConfig:
    PROVIDERS = {
        "google": {
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/drive.readonly"
            ]
        },
        "slack": {
            "auth_url": "https://slack.com/oauth/v2/authorize",
            "token_url": "https://slack.com/api/oauth.v2.access",
            "scopes": ["chat:write", "channels:read", "users:read"]
        },
        "github": {
            "auth_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "scopes": ["repo", "user"]
        },
        "notion": {
            "auth_url": "https://api.notion.com/v1/oauth/authorize",
            "token_url": "https://api.notion.com/v1/oauth/token",
            "scopes": ["read_content", "update_content"]
        }
    }

class CredentialManager:
    """
    Manages credentials storage in Redis.

    Note: MCP API keys have been moved to SQLite (see database.py).
    This class now only handles service credentials and OAuth flows.
    """

    def __init__(self, redis_url: str = "redis://redis:6379"):
        self.redis_client = redis.from_url(redis_url, decode_responses=True)
        self.credentials_prefix = "credentials:"
        self.oauth_state_prefix = "oauth_state:"
    
    def _generate_id(self) -> str:
        return secrets.token_urlsafe(16)
    
    def create_credential(self, user_id: str, cred_data: CredentialCreate) -> Credential:
        cred_id = self._generate_id()
        now = datetime.utcnow()
        
        credential = Credential(
            id=cred_id,
            name=cred_data.name,
            service=cred_data.service,
            type=cred_data.type,
            description=cred_data.description,
            created_at=now,
            updated_at=now,
            status="active"
        )
        
        cred_key = f"{self.credentials_prefix}{cred_id}"
        secret_key = f"{cred_key}:secret"
        metadata_key = f"{cred_key}:metadata"
        user_creds_key = f"user:{user_id}:credentials"
        
        self.redis_client.hset(metadata_key, mapping={
            "id": cred_id,
            "name": cred_data.name,
            "service": cred_data.service,
            "type": cred_data.type,
            "description": cred_data.description or "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "status": "active",
            "user_id": user_id
        })
        
        self.redis_client.set(secret_key, json.dumps(cred_data.credentials))
        
        self.redis_client.sadd(user_creds_key, cred_id)
        
        return credential
    
    def get_credential(self, cred_id: str, user_id: str) -> Optional[Credential]:
        metadata_key = f"{self.credentials_prefix}{cred_id}:metadata"
        metadata = self.redis_client.hgetall(metadata_key)
        
        if not metadata or metadata.get("user_id") != user_id:
            return None
        
        return Credential(
            id=metadata["id"],
            name=metadata["name"],
            service=metadata["service"],
            type=metadata["type"],
            description=metadata.get("description"),
            created_at=datetime.fromisoformat(metadata["created_at"]),
            updated_at=datetime.fromisoformat(metadata["updated_at"]),
            status=metadata["status"]
        )
    
    def get_credential_secret(self, cred_id: str, user_id: str) -> Optional[Dict]:
        metadata_key = f"{self.credentials_prefix}{cred_id}:metadata"
        metadata = self.redis_client.hgetall(metadata_key)
        
        if not metadata or metadata.get("user_id") != user_id:
            return None
        
        secret_key = f"{self.credentials_prefix}{cred_id}:secret"
        secret_data = self.redis_client.get(secret_key)
        
        if secret_data:
            return json.loads(secret_data)
        return None
    
    def list_credentials(self, user_id: str) -> List[Credential]:
        user_creds_key = f"user:{user_id}:credentials"
        cred_ids = self.redis_client.smembers(user_creds_key)
        
        credentials = []
        for cred_id in cred_ids:
            cred = self.get_credential(cred_id, user_id)
            if cred:
                credentials.append(cred)
        
        return sorted(credentials, key=lambda x: x.created_at, reverse=True)
    
    def update_credential(self, cred_id: str, user_id: str, update_data: CredentialUpdate) -> Optional[Credential]:
        metadata_key = f"{self.credentials_prefix}{cred_id}:metadata"
        metadata = self.redis_client.hgetall(metadata_key)
        
        if not metadata or metadata.get("user_id") != user_id:
            return None
        
        updates = {"updated_at": datetime.utcnow().isoformat()}
        
        if update_data.name:
            updates["name"] = update_data.name
        if update_data.description is not None:
            updates["description"] = update_data.description
        
        self.redis_client.hset(metadata_key, mapping=updates)
        
        if update_data.credentials:
            secret_key = f"{self.credentials_prefix}{cred_id}:secret"
            self.redis_client.set(secret_key, json.dumps(update_data.credentials))
        
        return self.get_credential(cred_id, user_id)
    
    def delete_credential(self, cred_id: str, user_id: str) -> bool:
        metadata_key = f"{self.credentials_prefix}{cred_id}:metadata"
        metadata = self.redis_client.hgetall(metadata_key)
        
        if not metadata or metadata.get("user_id") != user_id:
            return False
        
        secret_key = f"{self.credentials_prefix}{cred_id}:secret"
        user_creds_key = f"user:{user_id}:credentials"
        
        self.redis_client.delete(metadata_key, secret_key)
        self.redis_client.srem(user_creds_key, cred_id)
        
        return True
    
    def get_agent_credentials(self, agent_name: str, mcp_servers: List[str], user_id: str = None) -> Dict[str, Dict]:
        """
        Get credentials for an agent. Searches in two ways:
        1. Explicitly assigned credentials (agent:xxx:mcp:yyy:credential_id)
        2. User's credentials matched by name (for bulk-imported credentials)
        """
        agent_creds = {}

        # First, get explicitly assigned credentials
        for server in mcp_servers:
            cred_key = f"agent:{agent_name}:mcp:{server}:credential_id"
            cred_id = self.redis_client.get(cred_key)

            if cred_id:
                metadata_key = f"{self.credentials_prefix}{cred_id}:metadata"
                metadata = self.redis_client.hgetall(metadata_key)
                owner_id = metadata.get("user_id")

                if owner_id:
                    secret = self.get_credential_secret(cred_id, owner_id)
                    if secret:
                        agent_creds[server] = secret

        # Second, search user's credentials by name for bulk-imported credentials
        # This enables credentials named like "HEYGEN_API_KEY" to work automatically
        if user_id:
            user_credentials = self.list_credentials(user_id)
            for cred in user_credentials:
                # Get the secret data which contains the env var values
                secret = self.get_credential_secret(cred.id, user_id)
                if secret:
                    # Add each key-value pair from the credential
                    # This handles bulk imported credentials where name=ENV_VAR_NAME
                    for key, value in secret.items():
                        key_upper = key.upper()
                        if key_upper not in agent_creds:
                            agent_creds[key_upper] = value

        return agent_creds
    
    def assign_credential_to_agent(self, agent_name: str, mcp_server: str, cred_id: str, user_id: str) -> bool:
        cred = self.get_credential(cred_id, user_id)
        if not cred:
            return False
        
        cred_key = f"agent:{agent_name}:mcp:{mcp_server}:credential_id"
        self.redis_client.set(cred_key, cred_id)
        
        return True
    
    def create_oauth_state(self, user_id: str, provider: str, redirect_uri: str) -> str:
        state = secrets.token_urlsafe(32)
        state_key = f"{self.oauth_state_prefix}{state}"
        
        self.redis_client.setex(
            state_key,
            600,
            json.dumps({
                "user_id": user_id,
                "provider": provider,
                "redirect_uri": redirect_uri,
                "created_at": datetime.utcnow().isoformat()
            })
        )
        
        return state
    
    def verify_oauth_state(self, state: str) -> Optional[Dict]:
        state_key = f"{self.oauth_state_prefix}{state}"
        state_data = self.redis_client.get(state_key)
        
        if state_data:
            self.redis_client.delete(state_key)
            return json.loads(state_data)
        
        return None
    
    def build_oauth_url(self, provider: str, client_id: str, redirect_uri: str, state: str) -> Optional[str]:
        config = OAuthConfig.PROVIDERS.get(provider)
        if not config:
            return None
        
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(config["scopes"]),
            "response_type": "code",
            "access_type": "offline"
        }
        
        return f"{config['auth_url']}?{urlencode(params)}"
    
    async def exchange_oauth_code(
        self, 
        provider: str, 
        code: str, 
        client_id: str, 
        client_secret: str, 
        redirect_uri: str
    ) -> Optional[Dict]:
        config = OAuthConfig.PROVIDERS.get(provider)
        if not config:
            return None
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    config["token_url"],
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code"
                    },
                    headers={"Accept": "application/json"}
                )
                
                if response.status_code == 200:
                    tokens = response.json()

                    # Include client credentials for MCP compatibility
                    tokens['client_id'] = client_id
                    tokens['client_secret'] = client_secret

                    return tokens

            except Exception as e:
                print(f"OAuth exchange error: {e}")

        return None

    # ============================================================================
    # Credential Import with Conflict Resolution (Local Agent Deployment)
    # ============================================================================

    def get_credential_by_name(self, name: str, user_id: str) -> Optional[Credential]:
        """
        Find a credential by name for a specific user.

        Searches all credentials owned by user and returns first match.

        Args:
            name: Credential name to search for
            user_id: User ID to filter by

        Returns:
            Credential if found, None otherwise
        """
        user_creds = self.list_credentials(user_id)
        for cred in user_creds:
            if cred.name == name:
                return cred
        return None

    def import_credential_with_conflict_resolution(
        self,
        key: str,
        value: str,
        user_id: str
    ) -> Dict[str, str]:
        """
        Import a credential with conflict resolution.

        Resolution logic:
        - If no credential with same name exists: create new
        - If credential with same name and same value exists: reuse
        - If credential with same name but different value: create with suffix

        Args:
            key: Credential name (e.g., "API_KEY")
            value: Credential value
            user_id: User ID for ownership

        Returns:
            Dict with 'status' (created/reused/renamed), 'name', and optionally 'original'
        """
        existing = self.get_credential_by_name(key, user_id)

        if existing:
            # Check if values match
            existing_secret = self.get_credential_secret(existing.id, user_id)
            existing_value = None

            if existing_secret:
                # Handle different credential formats
                if isinstance(existing_secret, dict):
                    # For single-value credentials, value is stored as {"api_key": "value"}
                    # or {"token": "value"} or with the key name itself
                    existing_value = (
                        existing_secret.get("api_key") or
                        existing_secret.get("token") or
                        existing_secret.get(key) or
                        (list(existing_secret.values())[0] if existing_secret else None)
                    )
                else:
                    existing_value = existing_secret

            if existing_value == value:
                # Same name, same value -> reuse
                return {"status": "reused", "name": key}
            else:
                # Same name, different value -> create with suffix
                new_key = self._find_unique_credential_name(key, user_id)
                self._create_simple_credential(new_key, value, user_id)
                return {"status": "renamed", "name": new_key, "original": key}
        else:
            # New credential
            self._create_simple_credential(key, value, user_id)
            return {"status": "created", "name": key}

    def _find_unique_credential_name(self, base_name: str, user_id: str) -> str:
        """
        Find next available credential name with suffix.

        Args:
            base_name: Base credential name
            user_id: User ID

        Returns:
            Unique name like "API_KEY_2", "API_KEY_3", etc.
        """
        n = 2
        while True:
            candidate = f"{base_name}_{n}"
            if not self.get_credential_by_name(candidate, user_id):
                return candidate
            n += 1

    def _create_simple_credential(self, name: str, value: str, user_id: str) -> Credential:
        """
        Create a simple single-value credential.

        Args:
            name: Credential name
            value: Credential value
            user_id: User ID for ownership

        Returns:
            Created Credential object
        """
        # Import here to avoid circular imports
        from utils.helpers import infer_service_from_key, infer_type_from_key

        service = infer_service_from_key(name)
        cred_type = infer_type_from_key(name)

        cred_data = CredentialCreate(
            name=name,
            service=service,
            type=cred_type,
            credentials={"api_key": value, name: value},
            description="Auto-imported from local agent"
        )

        return self.create_credential(user_id, cred_data)

    # ============================================================================
    # MCP API Key Management - MOVED TO database.py for SQLite persistence
    # ============================================================================
    # All MCP API key methods have been migrated to src/backend/database.py
    # to use SQLite for persistent storage instead of Redis.

