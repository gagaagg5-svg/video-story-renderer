@echo off
cd /d "%~dp0"
py -3 video_story_renderer.py
if errorlevel 1 pause
