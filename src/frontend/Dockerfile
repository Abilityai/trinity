FROM node:26-alpine

WORKDIR /app

# Copy package files
COPY package.json ./

# Install dependencies
RUN npm install

# Copy application code
COPY . .

# Expose port
EXPOSE 3000

# Run the development server
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
