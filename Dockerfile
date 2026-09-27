FROM python:3.12-alpine
COPY tcl-airplay-proxy.py /app/tcl-airplay-proxy.py
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/tcl-airplay-proxy.py"]
