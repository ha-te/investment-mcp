"""Cloud HTTP transport. Deploy behind a trusted OAuth-capable reverse proxy.
This app deliberately refuses to start publicly without an explicitly configured
reverse proxy; it is NOT a turnkey ChatGPT OAuth implementation.
"""
import os
from server import register_mcp

if __name__ == '__main__':
    if os.getenv('INVESTMENT_TRUSTED_AUTH_PROXY') != 'true':
        raise SystemExit('Refusing remote MCP startup: configure an OAuth-capable auth proxy first; set INVESTMENT_TRUSTED_AUTH_PROXY=true only behind that proxy')
    mcp = register_mcp()
    mcp.settings.host = '0.0.0.0'
    mcp.settings.port = int(os.getenv('PORT', '8000'))
    mcp.run(transport='streamable-http')
