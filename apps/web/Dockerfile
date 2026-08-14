FROM nginx:alpine

COPY apps/web/index.html /usr/share/nginx/html/index.html
COPY apps/web/app.js /usr/share/nginx/html/app.js
COPY apps/web/styles.css /usr/share/nginx/html/styles.css

COPY apps/web/default.conf /etc/nginx/conf.d/default.conf