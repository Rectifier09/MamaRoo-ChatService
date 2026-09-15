"""
Central configuration for the test webapp. All values can be overridden via
environment variables (or a .env file -- see .env.example). See
docs/superpowers/specs/2026-09-15-test-webapp-design.md for what each is for.
"""
import os
from dotenv import load_dotenv

load_dotenv()

WEBAPP_USERNAME = os.environ.get("WEBAPP_USERNAME", "")
WEBAPP_PASSWORD = os.environ.get("WEBAPP_PASSWORD", "")
CHATSERVICE_URL = os.environ.get("CHATSERVICE_URL", "")
CHATSERVICE_API_KEY = os.environ.get("CHATSERVICE_API_KEY", "")
