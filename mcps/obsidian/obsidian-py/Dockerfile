FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OBSIDIAN_MCP_HOST=0.0.0.0 OBSIDIAN_MCP_PORT=8765

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER 99:100
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"OBSIDIAN_MCP_PORT\",\"8765\")}/health')" || exit 1

ENTRYPOINT ["obsidian-mcp"]
CMD ["--transport", "http"]
