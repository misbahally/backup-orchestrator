FROM nginx:alpine

ARG BUILD_REF=dev

COPY apps/web/index.html /usr/share/nginx/html/index.html
COPY apps/web/app.js /usr/share/nginx/html/app.js
COPY apps/web/styles.css /usr/share/nginx/html/styles.css

COPY apps/web/default.conf /etc/nginx/conf.d/default.conf

RUN sed -i "s|__BUILD_REF__|${BUILD_REF}|g" /usr/share/nginx/html/app.js