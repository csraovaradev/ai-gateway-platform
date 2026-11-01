FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY aigw ./aigw
RUN pip install --no-cache-dir --prefix=/install ".[bedrock,redis]"

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 GATEWAY_CONFIG=/etc/aigw/gateway.yaml
COPY --from=build /install /usr/local
RUN useradd --uid 10001 --no-create-home aigw
USER 10001
EXPOSE 8080
CMD ["uvicorn", "--factory", "aigw.main:create_app", "--host", "0.0.0.0", "--port", "8080"]
