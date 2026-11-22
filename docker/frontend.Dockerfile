# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS build
WORKDIR /workspace/autohunt
ENV NEXT_TELEMETRY_DISABLED=1 AUTOHUNT_DOCKER_BUILD=1
RUN npm install --global pnpm@11.19.0
COPY autohunt/package.json autohunt/pnpm-lock.yaml autohunt/pnpm-workspace.yaml ./
COPY autohunt/frontend/package.json ./frontend/package.json
RUN pnpm install --frozen-lockfile
COPY autohunt/frontend ./frontend
# Keep the copy below valid even when the application has no public assets.
RUN mkdir -p frontend/public && pnpm --dir frontend build --webpack

FROM node:24-bookworm-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
COPY --from=build --chown=node:node /workspace/autohunt/frontend/.next/standalone ./
COPY --from=build --chown=node:node /workspace/autohunt/frontend/.next/static ./frontend/.next/static
COPY --from=build --chown=node:node /workspace/autohunt/frontend/public ./frontend/public
COPY --chown=node:node docker/frontend-entrypoint.mjs ./frontend-entrypoint.mjs
USER node
EXPOSE 3000
CMD ["node", "/app/frontend-entrypoint.mjs"]
