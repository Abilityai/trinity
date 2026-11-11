FROM node:26-alpine

WORKDIR /app

# Copy package files
COPY package.json package-lock.json ./

# Install from the tracked lockfile (#4435). npm ci is lockfile-exact and fails on
# drift. A missing native binary for the container platform is fixed in
# package-lock.json, never by deleting the lockfile.
RUN npm ci

# Copy application code
COPY . .

# Expose port
EXPOSE 80

# Run the development server
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
