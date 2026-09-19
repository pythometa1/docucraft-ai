# DocuMind API.
#
# Two stages so the image that ships does not carry a compiler. The wheels are
# built once against the same Debian base the runtime uses, which matters for
# lxml and psycopg: both link against system libraries, and a wheel built on a
# different base is the kind of thing that works locally and segfaults in the
# cluster.
FROM python:3.12-slim-bookworm AS build

# lxml needs libxml2/libxslt headers; psycopg needs libpq. Only in this stage.
RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential libxml2-dev libxslt1-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


FROM python:3.12-slim-bookworm AS runtime

# Runtime libraries, plus LibreOffice Writer for DOCX->PDF conversion -- the
# invoice service delivers PDFs, so the path that used to have no callers now
# does. Writer-only with --no-install-recommends keeps the addition to a few
# hundred MB rather than the full suite's ~1GB; fonts-dejavu covers the Latin
# range plus the currency glyphs invoices print. `pdf_renderer.render_pdf`
# finds /usr/bin/soffice via SOFFICE_CANDIDATES and, absent this package,
# answers 503 PDF_UNAVAILABLE -- so dev machines without it keep working.
RUN apt-get update && apt-get install --no-install-recommends -y \
        libxml2 libxslt1.1 libpq5 curl \
        libreoffice-writer fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

# Not root. A template is an untrusted zip from outside the trust boundary and
# the first thing this service does is open it and walk its XML.
RUN useradd --create-home --uid 10001 documind

WORKDIR /app
COPY --from=build /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY --chown=documind:documind alembic.ini ./
COPY --chown=documind:documind alembic ./alembic
COPY --chown=documind:documind app ./app
COPY --chown=documind:documind scripts ./scripts

# Uploaded templates, source files and generated documents. A volume in any real
# deployment -- object storage is what §14 asks for, and a container filesystem
# is not durable.
RUN mkdir -p /var/lib/documind && chown documind:documind /var/lib/documind
ENV STORAGE_DIR=/var/lib/documind

USER documind

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# `readyz` checks the database and Redis; `healthz` only says the process is up.
# The orchestrator wants the first for traffic and the second for liveness, or a
# database blip turns into a restart loop that cannot fix it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/readyz || exit 1

EXPOSE 8000

# Migrations are NOT run here. A container that migrates on start races every
# other replica of itself during a rolling deploy; run `alembic upgrade head`
# as a pre-deploy job and let the app assume the schema is current.
#
# --no-server-header: no response needs to say which server produced it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--no-server-header"]
