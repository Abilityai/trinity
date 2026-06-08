# Trinity MCP Server
# Provides MCP interface for agent orchestration

FROM node:22-alpine

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies (use npm install instead of ci for flexibility)
RUN npm install --production

# Copy TypeScript config and source
COPY tsconfig.json ./
COPY src ./src

# Install dev dependencies for build
RUN npm install && npm run build && npm prune --production

# Run as non-root (CSO hardening, issue #874).
# node:22-alpine ships a built-in `node` user at UID 1000.
RUN chown -R node:node /app
USER node

# Expose MCP server port
EXPOSE 8080

# Environment variables (with defaults)
ENV NODE_ENV=production
ENV MCP_PORT=8080
ENV TRINITY_API_URL=http://backend:8000

# Health check
# #443: probe /health, not /mcp. The MCP endpoint rejects HEAD requests
# with 400 BadRequest (wget --spider sends HEAD), so the previous probe
# reported the container as `unhealthy` despite the service serving
# traffic. /health returns 200 to both HEAD and GET.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:8080/health || exit 1

# Run the server
CMD ["node", "dist/index.js"]
