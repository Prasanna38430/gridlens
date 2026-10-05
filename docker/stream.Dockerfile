# The producer only. Slim python rather than the airflow image: the stream
# profile runs without airflow, and this one is a few hundred megabytes rather
# than nearly three gigabytes.
#
# gridlens itself is bind mounted onto PYTHONPATH, as in the airflow image, so
# a change to the producer is a restart rather than a rebuild.
FROM python:3.12-slim

COPY stream-requirements.txt /tmp/stream-requirements.txt
RUN pip install --no-cache-dir -r /tmp/stream-requirements.txt \
    && useradd --create-home --uid 50000 producer

USER producer
ENV PYTHONPATH=/opt/gridlens/src PYTHONUNBUFFERED=1
CMD ["python", "-m", "gridlens.stream.producer"]
