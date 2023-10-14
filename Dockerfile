FROM python:3.8
WORKDIR /app

COPY ./requirements.txt /app

ENV PYTHONUNBUFFERED 1
RUN apt-get -y update
RUN apt-get install -y ffmpeg
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .