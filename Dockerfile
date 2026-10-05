FROM node:22-alpine AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install . \
    && groupadd --gid 10001 sitg \
    && useradd --uid 10001 --gid sitg --no-create-home sitg \
    && mkdir /app/logs \
    && chown sitg:sitg /app/logs
COPY alembic.ini ./
COPY migrations/ ./migrations/
RUN chmod -R a+rX /app/migrations
COPY --from=web-build /web/dist/ ./web/dist/
USER sitg
CMD ["sitg-server", "--host", "0.0.0.0"]
