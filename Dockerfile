FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/London

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        iputils-ping \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "collector_shaprepoint.py"]