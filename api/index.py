"""Vercel 서버리스 진입점: 루트의 app.py에 있는 Flask 앱을 그대로 씀."""
import os
import sys
from urllib.parse import unquote_to_bytes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402


class _DecodePath:
    """Vercel은 한글 주소(/event/블랙컴뱃%2016)를 인코딩된 채로 넘겨서 Flask가 못 찾음 → 풀어서 전달"""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if "%" in path:
            environ["PATH_INFO"] = unquote_to_bytes(path).decode("latin-1")
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _DecodePath(app.wsgi_app)
