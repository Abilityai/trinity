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

# Expose MCP server port
EXPOSE 8080

# Environment variables (with defaults)
ENV NODE_ENV=production
ENV MCP_PORT=8080
ENV TRINITY_API_URL=http://backend:8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost:8080/mcp || exit 1

# Run the server
CMD ["node", "dist/index.js"]
