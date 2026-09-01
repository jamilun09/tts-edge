FROM python:3.11-slim

WORKDIR /app

# Install dependency dulu (supaya build lebih cepat kalau kode berubah tapi
# dependency tidak berubah, Docker akan pakai cache untuk langkah ini)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin semua file proyek ke dalam container
COPY . .

# Port yang akan dipakai server di dalam container.
# WAJIB SAMA dengan yang nanti diisi di kolom "Port" pada dashboard Back4app.
EXPOSE 8080
ENV PORT=8080

CMD ["python", "server.py"]
