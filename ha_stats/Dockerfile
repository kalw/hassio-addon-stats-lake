# Stage 1: install Python deps into a venv
FROM python:3.12-alpine AS build
WORKDIR /build
RUN python -m venv /venv
COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

# Stage 2: slim runtime
FROM python:3.12-alpine
RUN apk add --no-cache rclone
COPY --from=build /venv /venv
ENV PATH="/venv/bin:$PATH"
WORKDIR /app
COPY ha_stats.py .
COPY run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
