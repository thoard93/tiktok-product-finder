"""
PRISM — Entry Point
Starts the Flask application via the app factory.
Used by Gunicorn (Procfile / render.yaml) and local dev.
"""

from app import app  # noqa: F401 — created by app factory in app/__init__.py

if __name__ == '__main__':
    app.run(debug=True, port=5000)
