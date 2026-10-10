FROM node:22-alpine AS build

WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.17.1 --activate

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . ./
RUN pnpm build

FROM nginx:1.28-alpine AS runtime

ARG APP_VERSION=0.4.1
ARG SOURCE_REVISION=unknown

LABEL org.opencontainers.image.title="kira-context-memory-frontend" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"

# Only the upstream is substituted; nginx request variables keep their runtime meaning.
ENV NGINX_ENVSUBST_FILTER=GATEWAY_UPSTREAM

RUN rm -f /etc/nginx/conf.d/default.conf \
    && sed -i 's#^pid .*#pid /tmp/nginx.pid;#; s#^user .*#\# user directive omitted for non-root runtime#' /etc/nginx/nginx.conf \
    && chown -R nginx:nginx /etc/nginx/conf.d /var/cache/nginx

COPY docker-entrypoint.d/05-validate-env.sh /docker-entrypoint.d/05-validate-env.sh
COPY nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

RUN chmod 0555 /docker-entrypoint.d/05-validate-env.sh \
    && chown -R nginx:nginx /usr/share/nginx/html

# Numeric IDs match the Kubernetes runAsUser/runAsGroup contract directly.
USER 101:101

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
