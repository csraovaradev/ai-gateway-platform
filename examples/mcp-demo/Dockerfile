FROM python:3.12-slim
RUN pip install --no-cache-dir --default-timeout=120 "mcp>=2" && useradd --uid 10001 --no-create-home mcp
COPY server.py /app/server.py
USER 10001
ENV MCP_HOST=0.0.0.0
EXPOSE 9000
CMD ["python", "/app/server.py"]
