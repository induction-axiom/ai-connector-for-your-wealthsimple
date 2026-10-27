FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY wealthsimple_connector ./wealthsimple_connector
COPY static ./static
COPY version.json ./
# The app user can't write __pycache__ here, so compile now rather than on every cold start.
RUN python -m compileall -q .
RUN useradd --uid 10001 --create-home app
USER 10001
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn app:production_app --factory --host 0.0.0.0 --port ${PORT:-8080} --no-access-log"]
