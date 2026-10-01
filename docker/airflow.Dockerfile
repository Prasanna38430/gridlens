# The stock airflow image has boto3 and nothing this project needs. Extending
# it is the supported way to add dependencies, and it keeps the dag folder free
# of install-time tricks.
#
# gridlens itself is not installed here on purpose. It is bind mounted onto
# PYTHONPATH at runtime, so editing a module does not mean rebuilding an image.
FROM apache/airflow:3.3.1-python3.12

COPY requirements.txt /tmp/gridlens-requirements.txt

RUN pip install --no-cache-dir -r /tmp/gridlens-requirements.txt

# dbt gets a virtualenv of its own, for the restatement audit. It is a command
# line tool the dags run, not a library they import. Installed beside airflow
# it would downgrade four packages airflow ships, PyAthena 3.35.4 to 3.34.0
# among them (pip install --dry-run, measured), with no error until something
# used them. Root only to create the directory; nothing writes to it at
# runtime.
COPY --from=transform requirements.txt /tmp/dbt-requirements.txt

USER root
RUN python -m venv /opt/dbt \
    && /opt/dbt/bin/pip install --no-cache-dir -r /tmp/dbt-requirements.txt
USER airflow
