#!/bin/bash
export CONFIG_PATH=/tmp/equitybuddy/config.yaml
export DB_PATH=/tmp/equitybuddy/data/equitybuddy.db
export COOKIES_PATH=/tmp/equitybuddy/cookies/twitter_cookies.json

exec .venv/bin/uvicorn app.main:app --port 7003
