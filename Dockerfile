# The public demo. Deliberately does NOT install the package or the test
# corpus: the container needs the crawler and a web server, nothing else.
FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir "httpx>=0.27" "selectolax>=0.3.21"
COPY minicrawl ./minicrawl

# Without this the server starts under the LOCAL policy, which allows loopback
# targets. A deployment that forgets the flag gets the safe default, not the
# permissive one — but this is the deployment, so it says so explicitly.
ENV MINICRAWL_PUBLIC=1 PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["sh", "-c", "python -m minicrawl.web --host 0.0.0.0 --port ${PORT:-8000} --no-browser"]
