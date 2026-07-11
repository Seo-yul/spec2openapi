# Reference MCP runtime image (optional; requires the [mcp] extra).
# The primary deliverable of spec2openapi is the spec itself - this image
# exists as a working reference for an openapi->MCP runtime that honors
# the x-soap extensions. Build once; swap the spec via ConfigMap.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[mcp]"

RUN useradd --system --uid 1001 --no-create-home app
USER app

EXPOSE 8000

# Spec path and flags are CMD so k8s manifests can override them.
ENTRYPOINT ["spec2openapi", "serve"]
CMD ["/config/openapi.yaml", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
