FROM python:3.12-slim

WORKDIR /app

COPY app/Report/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

EXPOSE 5000

CMD ["sh", "-c", "flask --app run db stamp head && gunicorn -w 2 --forwarded-allow-ips='*' --access-logfile - --error-logfile - -b 0.0.0.0:${PORT:-5000} run:app"]