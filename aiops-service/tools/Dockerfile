FROM alpine:3.20
RUN apk add --no-cache stress-ng python3 py3-pip curl bash \
    && pip install --no-cache-dir pymysql --break-system-packages
CMD ["sleep", "infinity"]
