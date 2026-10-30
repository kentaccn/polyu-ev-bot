FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY polyu_ev_bot/ ./polyu_ev_bot/
RUN mkdir -p /app/data && chown 65534:65534 /app/data
VOLUME ["/app/data"]
USER 65534:65534
CMD ["python", "-m", "polyu_ev_bot"]
