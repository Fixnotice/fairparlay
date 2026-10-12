@echo off
cd /d "%~dp0docs"
start "" http://localhost:8000/
py -m http.server 8000
