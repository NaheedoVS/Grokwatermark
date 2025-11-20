# Watermark Bot

A Telegram bot for adding watermarks to videos. Now with dynamic text overlays!

## Features
- Image & Text watermarks (new: colors, sizes, toggle)
- FFmpeg presets
- MongoDB storage
- Heroku deploy

## Setup
1. Fork this repo.
2. Set env vars.
3. Add FFmpeg buildpack: `heroku buildpacks:add https://github.com/jonathanong/heroku-buildpack-ffmpeg-latest.git`
4. `heroku ps:scale worker=1`
5. Deploy!

## New: Text Watermark
Use /settings to set text, color (e.g., #FF0000), size (px), and toggle.

## Usage
/start - Welcome
Upload video - Process
/settings - Configure

Demo: @VideoWatermark_Bot
Support: @DevsZone
