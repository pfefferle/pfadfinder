"""A requests transport adapter that replays canned responses and records requests."""

import json

import requests
from requests.adapters import BaseAdapter


class FakeAdapter(BaseAdapter):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)  # (status, body dict | Exception, headers)
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        status, body, headers = self.responses.pop(0)
        if isinstance(body, Exception):
            raise body
        resp = requests.Response()
        resp.status_code = status
        resp._content = json.dumps(body).encode()
        resp.headers.update({"Content-Type": "application/json", **headers})
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


def session_with(*responses):
    s = requests.Session()
    adapter = FakeAdapter(responses)
    s.mount("https://", adapter)
    return s, adapter
