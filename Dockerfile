FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
RUN python -c "import subprocess, tomllib; subprocess.check_call(['python', '-m', 'pip', 'install', '--no-cache-dir', *tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']])" \
    && useradd --uid 10001 --create-home sshfm \
    && mkdir /data && chown sshfm:sshfm /data
COPY README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
COPY tests ./tests
USER sshfm
EXPOSE 2222
CMD ["python", "-m", "sshfm"]
