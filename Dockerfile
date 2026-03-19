FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir requests pyyaml packaging && \
    apt-get update && apt-get install -y skopeo && rm -rf /var/lib/apt/lists/*

COPY release_monitor.py .

RUN mkdir /app/config && \
    useradd -u 1000 monitor && \
    chown -R monitor:monitor /app

USER monitor

VOLUME ["/config"]

CMD ["python", "-u", "release_monitor.py"]