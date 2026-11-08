@echo off
setlocal
set "PY=C:\Users\zwl\AppData\Local\Programs\Python\Python39\pythonw.exe"
if not exist "%PY%" set "PY=pythonw"
start "" "%PY%" "%~dp0app.py"
