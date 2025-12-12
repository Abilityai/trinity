"""
Trinity MCP injection service for agent-to-agent collaboration.
"""
import os
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def inject_trinity_mcp_if_configured() -> bool:
    """
    Inject Trinity MCP server into agent's .mcp.json if credentials are configured.

    This enables agent-to-agent communication via the Trinity platform.
    Called on agent startup.
    """
    trinity_mcp_url = os.getenv("TRINITY_MCP_URL")
    trinity_mcp_api_key = os.getenv("TRINITY_MCP_API_KEY")

    if not trinity_mcp_url or not trinity_mcp_api_key:
        logger.info("Trinity MCP not configured - skipping injection")
        return False

    home_dir = Path("/home/developer")
    mcp_file = home_dir / ".mcp.json"

    # Trinity MCP server configuration using HTTP transport
    trinity_mcp_entry = {
        "trinity": {
            "type": "http",
            "url": trinity_mcp_url,
            "headers": {
                "Authorization": f"Bearer {trinity_mcp_api_key}"
            }
        }
    }

    try:
        # Read existing .mcp.json if it exists
        if mcp_file.exists():
            content = mcp_file.read_text()
            if content.strip():
                mcp_config = json.loads(content)
            else:
                mcp_config = {"mcpServers": {}}
        else:
            mcp_config = {"mcpServers": {}}

        # Ensure mcpServers key exists
        if "mcpServers" not in mcp_config:
            mcp_config["mcpServers"] = {}

        # Add Trinity MCP (overwrite if exists)
        mcp_config["mcpServers"]["trinity"] = trinity_mcp_entry["trinity"]

        # Write back to file
        mcp_file.write_text(json.dumps(mcp_config, indent=2))
        logger.info(f"Injected Trinity MCP server into {mcp_file}")
        return True

    except Exception as e:
        logger.warning(f"Failed to inject Trinity MCP: {e}")
        return False
