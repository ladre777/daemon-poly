FROM python:3.11-slim

# ffmpeg lets yt-dlp merge separate video+audio streams, which is what makes
# the higher rungs of the quality ladder reachable on most sites.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY vidgrab/requirements.txt ./vidgrab/requirements.txt
RUN pip install --no-cache-dir -r vidgrab/requirements.txt

COPY vidgrab ./vidgrab
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "vidgrab"]
