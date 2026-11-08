FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 MPLBACKEND=Agg
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY configs ./configs
RUN pip install --no-deps . && useradd --create-home --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "tsp_hub.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
