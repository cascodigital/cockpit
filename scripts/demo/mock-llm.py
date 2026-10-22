#!/usr/bin/env python3
"""Tiny OpenAI-compatible mock server for demos and keyless UI testing.

Serves POST /chat/completions and answers every request with a canned
markdown answer derived from the prompt. Lets you exercise the Ask-the-
Panopticon button and the LLM reranker without any API key:

    python scripts/demo/mock-llm.py --port 9999

Note: the current app/cockpit.py sends rerank/ask requests to the fixed
https://api.openai.com/v1 endpoint, so pointing the server at this stub requires
re-adding a configurable base URL (an earlier OPENAI_BASE_URL option was removed).
"""
import argparse
import json
import http.server
import re


class MockHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        user_msg = ''
        for m in body.get('messages', []):
            if m.get('role') == 'user':
                user_msg = m.get('content', '')

        if '"order"' in json.dumps(body):  # reranker asks for JSON {"order": [...]}
            n = len(re.findall(r'^\[\d+\]', user_msg, re.M)) or 5
            content = json.dumps({"order": list(range(n))})
        else:
            question = user_msg.rsplit('QUESTION:', 1)[-1].strip() or 'your question'
            dates = sorted(set(re.findall(r'### (\d{4}-\d{2}-\d{2})', user_msg)), reverse=True)
            cited = f" (see {dates[0]})" if dates else ""
            content = (
                f"**Short answer:** yes — the journal covers *{question}*{cited}.\n\n"
                f"- The relevant entries are summarized above in the seeded demo journal.\n"
                f"- This response comes from `scripts/demo/mock-llm.py`, not a real model.\n\n"
                f"Point `OPENAI_BASE_URL` at a real OpenAI-compatible server for actual answers."
            )

        resp = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, fmt, *args):
        print(f"[mock-llm] {fmt % args}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=9999)
    args = parser.parse_args()
    print(f"Mock OpenAI-compatible server on :{args.port} (POST /chat/completions)")
    http.server.ThreadingHTTPServer(('0.0.0.0', args.port), MockHandler).serve_forever()


if __name__ == '__main__':
    main()
