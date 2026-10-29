"""A real OpenAI-compatible server used by the tests. No mocks."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeVLLM:
    def __init__(self, latency=0.0, port=0):
        self.latency = latency
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.hits = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.endswith("/models"):
                    body = json.dumps(
                        {"data": [{"id": "fake-model"}, {"id": "other-model"}]}
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")

                with fake.lock:
                    fake.active += 1
                    fake.hits += 1
                    fake.max_active = max(fake.max_active, fake.active)
                try:
                    time.sleep(fake.latency)
                    prompt = payload["messages"][-1]["content"]
                    if "FAIL" in prompt:
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        body = b'{"error": {"message": "engine exploded"}}'
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    if payload.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        for piece in ["Hello", " from", " the", " fake"]:
                            chunk = {"choices": [{"delta": {"content": piece}}]}
                            self.wfile.write(
                                ("data: " + json.dumps(chunk) + "\n\n").encode()
                            )
                            self.wfile.flush()
                            time.sleep(0.005)
                        usage = {
                            "choices": [],
                            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                        }
                        self.wfile.write(("data: " + json.dumps(usage) + "\n\n").encode())
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    else:
                        body = json.dumps(
                            {
                                "choices": [
                                    {
                                        "message": {
                                            "role": "assistant",
                                            "content": "Hello from the fake",
                                        }
                                    }
                                ],
                                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                            }
                        ).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                finally:
                    with fake.lock:
                        fake.active -= 1

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
