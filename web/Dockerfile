# Build the dashboard, then serve the static output from nginx.
#
# Two stages so the runtime image carries no Node.js, no node_modules and no
# source: about 50 MB of nginx and assets rather than a gigabyte of toolchain.

FROM node:22-alpine AS build

WORKDIR /app

# Dependencies first, so a source edit does not invalidate the install layer.
COPY package.json package-lock.json* ./
# npm ci needs a lockfile. Falling back to install keeps the image buildable in
# a fresh checkout where the lockfile has not been generated yet.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

# 127.0.0.1 rather than localhost: `listen 80` binds IPv4 only, and busybox wget
# resolves localhost to ::1 first, so the check fails against a perfectly healthy
# server.
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=10s \
  CMD wget --spider -q http://127.0.0.1/index.html || exit 1
