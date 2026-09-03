# The stock airflow image has boto3 and nothing this project needs. Extending
# it is the supported way to add dependencies, and it keeps the dag folder free
# of install-time tricks.
#
# gridlens itself is not installed here on purpose. It is bind mounted onto
# PYTHONPATH at runtime, so editing a module does not mean rebuilding an image.
FROM apache/airflow:3.3.1-python3.12

COPY requirements.txt /tmp/gridlens-requirements.txt

RUN pip install --no-cache-dir -r /tmp/gridlens-requirements.txt
