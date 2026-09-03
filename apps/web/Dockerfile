FROM nginx:alpine

ARG BUILD_REF=dev

COPY apps/web/index.html /usr/share/nginx/html/index.html
COPY apps/web/app.js /usr/share/nginx/html/app.js
COPY apps/web/styles.css /usr/share/nginx/html/styles.css

COPY apps/web/default.conf.template /etc/nginx/templates/default.conf.template

RUN sed -i "s|__BUILD_REF__|${BUILD_REF}|g" /usr/share/nginx/html/app.js

# Defaults match the docker-compose service name; override in ECS task defs
# (e.g. Service Connect/Cloud Map DNS name, and 169.254.169.253 for the VPC resolver).
ENV API_UPSTREAM_HOST=backup-orchestrator-api
ENV API_UPSTREAM_PORT=8001
ENV DNS_RESOLVER=127.0.0.11