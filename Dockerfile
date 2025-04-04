FROM python:3.13
COPY . .

RUN apt-get update && apt-get upgrade -y
RUN apt-get install aria2 -y
RUN pip --no-cache-dir install -U pip && pip --no-cache-dir install -r requirements.txt && rm README.md

RUN aria2c https://github.com/official-stockfish/Stockfish/releases/download/sf_17/stockfish-ubuntu-x86-64-avx2.tar
RUN tar -xf stockfish-*.tar && rm stockfish-*.tar
RUN mv stockfish/stockfish-* engines/stockfish.17 && rm -r stockfish 
CMD chmod +x token-enabler.sh && ./token-enabler.sh && python web_interface.py 
