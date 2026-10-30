# OfferFlow AI frontend (local development: Next.js dev server).
FROM node:22-slim

WORKDIR /srv/app

COPY package.json package-lock.json ./
RUN npm install

COPY . ./

EXPOSE 3000

# 0.0.0.0 binds inside the container; Compose publishes loopback-only.
CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0", "--port", "3000"]
