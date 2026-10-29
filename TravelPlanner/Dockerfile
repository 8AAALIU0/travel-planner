FROM node:24-bookworm-slim AS node_tools
RUN npm install --global tuniu-cli@1.1.4
FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DJANGO_SETTINGS_MODULE=config.render_settings
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --create-home travel
COPY --from=node_tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node_tools /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/tuniu-cli/bin/tuniu.js /usr/local/bin/tuniu
COPY requirements-render.txt ./
RUN pip install --no-cache-dir -r requirements-render.txt
COPY --chown=travel:travel . .
RUN python manage.py collectstatic --noinput --settings=config.build_settings && node /usr/local/lib/node_modules/tuniu-cli/bin/tuniu.js --version
USER travel
EXPOSE 10000
CMD ["python", "deploy/start_render.py"]
