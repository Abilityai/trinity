FROM node:24-alpine

WORKDIR /app

# Copy package files
COPY package.json package-lock.json ./

# Install dependencies (use npm install to get correct native binaries for container arch)
RUN rm -f package-lock.json && npm install

# Copy application code
COPY . .

# Expose port
EXPOSE 80

# Run the development server
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
