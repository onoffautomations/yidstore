# Self-contained base image: newer Supervisor versions no longer pass a
# BUILD_FROM build argument unless the add-on ships a build.yaml.
FROM python:3.13-alpine

COPY run.py /run.py

CMD ["python3", "-u", "/run.py"]
