# Gemini Podcasts for Cloud Run.
#
# One uvicorn worker on purpose: live rooms (who's listening, the audio being
# voiced) live in this process's memory. See "Scaling" in the README.
FROM python:3.13-slim

# OPENBLAS_NUM_THREADS=1: numpy (mixing memo pods) needs no BLAS threads, and
#   OpenBLAS otherwise reserves buffers for every CPU when it loads.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENBLAS_NUM_THREADS=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static
# The prepackaged music library every pod's theme comes from (tools/build_music_library.py).
COPY music ./music

RUN useradd --create-home --uid 10001 podcasts && mkdir -p /srv/data && chown podcasts /srv/data
USER podcasts

ENV PORT=8080
EXPOSE 8080

# --ws-per-message-deflate false: audio barely compresses, and compressing
#   it separately for every listener costs CPU for nothing.
# --timeout-graceful-shutdown 3: Cloud Run allows 10 s after SIGTERM; the rest
#   goes to saving live shows (see Rooms.shutdown).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --ws-per-message-deflate false --ws-max-size 65536 --timeout-graceful-shutdown 3"]
