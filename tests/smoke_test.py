"""
End-to-end smoke test against a real local HTTP server.

This proves the scraper actually talks to a live socket, parses a real
response, and that the conversion auditor produces sane output on a real
HTTP exchange. The LLM is still stubbed (no Anthropic key needed).

Run:
    python tests/smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Force a writable output dir + dummy keys
os.environ["OUTPUT_DIR"] = "/tmp/audit_smoke_out"
os.environ["ANTHROPIC_API_KEY"] = "smoke-test-key"
os.environ["PAGESPEED_API_KEY"] = ""

import config  # noqa: E402
config.OUTPUT_DIR = "/tmp/audit_smoke_out"
os.makedirs(config.OUTPUT_DIR, exist_ok=True)

import scraper  # noqa: E402
import analyzer  # noqa: E402
import conversion_audit  # noqa: E402
import output  # noqa: E402
import sender  # noqa: E402
from audit_agent import process_single  # noqa: E402


GOOD_HTML = """
<!DOCTYPE html>
<html><head>
  <title>Acme Plumbing — 24/7 Emergency in Phoenix, AZ</title>
  <meta name="description" content="Licensed, bonded, and insured plumbers in Phoenix. Free estimates.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="http://localhost/">
</head><body>
  <header>
    <a href="tel:+16025551234">(602) 555-1234</a>
    <a class="cta primary" href="/quote">Get Free Estimate</a>
  </header>
  <h1>Phoenix's Trusted 24/7 Plumber</h1>
  <p>Licensed, bonded and insured. Serving the Phoenix metro area since 1998.</p>
  <p>4.8 stars on Google · Best of Phoenix 2024</p>
  <div class="testimonial">
    "Mike showed up at 11pm on a Saturday and fixed our burst pipe in 40 minutes.
     Honest pricing, real professional." — Janet R., Tempe
  </div>
  <a href="mailto:owner@acmeplumbing.example">owner@acmeplumbing.example</a>
  <footer>© 2026 Acme Plumbing</footer>
</body></html>
"""

SLOW_HTML = "<html><body>slowly loaded page</body></html>"


class GoodHandler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):
        if self.path == "/slow":
            time.sleep(0.3)
            html = SLOW_HTML
        elif self.path == "/500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"server error")
            return
        elif self.path == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/")
            self.end_headers()
            return
        else:
            html = GOOD_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Server", "smoke-test/1.0")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


def _start_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), GoodHandler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"


def _check(name: str, condition: bool, detail: str = ""):
    icon = "OK" if condition else "FAIL"
    print(f"  [{icon}] {name}" + (f"  — {detail}" if detail else ""))
    if not condition:
        raise AssertionError(name)


def main():
    print("\n=== END-TO-END SMOKE TEST (real HTTP, stubbed LLM) ===\n")

    server, base = _start_server()
    try:
        # 1. Real fetch + SEO + tech stack
        print("1. Real HTTP fetch")
        r = scraper.fetch_html(base + "/")
        _check("status 200", r["status_code"] == 200)
        _check("html received", bool(r["html"]) and "Acme Plumbing" in r["html"])
        _check("load time recorded", r["load_time_ms"] is not None,
               f"{r['load_time_ms']} ms")

        seo = scraper.extract_seo_signals(r["html"], base)
        _check("title parsed", "Acme Plumbing" in (seo["title"] or ""))
        _check("h1 count >= 1", seo["h1_count"] >= 1)
        _check("viewport present", seo["has_viewport"])

        tech = scraper.detect_tech_stack(r["html"])
        _check("tech detection runs", isinstance(tech, dict))

        # 2. Conversion audit on the real response
        print("\n2. Conversion audit on real fetch")
        audit = conversion_audit.audit_conversion(
            r["html"], base, niche="plumber", location="Phoenix, AZ"
        )
        _check("h1 captured", audit.above_fold["h1"] is not None)
        _check("primary CTA found",
               audit.primary_cta["primary_cta_text"] is not None,
               f"\"{audit.primary_cta['primary_cta_text']}\"")
        _check("phone clickable detected", audit.local["has_phone_clickable"])
        _check("city in copy", audit.local["city_appears_on_page"])
        _check("niche check ran", audit.niche_check["checked"])
        _check("audit serializes to JSON", isinstance(json.dumps(audit.to_dict()), str))

        # 3. HTTP error path (500)
        print("\n3. HTTP error handling")
        r500 = scraper.fetch_html(base + "/500")
        _check("500 captured", r500["status_code"] == 500)
        _check("html None on 500", r500["html"] is None)
        _check("error message set", "500" in (r500["error"] or ""))

        # 4. Redirect handling
        print("\n4. Redirect handling")
        rred = scraper.fetch_html(base + "/redirect")
        _check("redirect followed to 200", rred["status_code"] == 200)
        _check("html received after redirect", bool(rred["html"]))

        # 5. Full process_single with stubbed LLM, real HTTP
        print("\n5. Full pipeline through process_single (stubbed LLM)")
        analysis = json.dumps({
            "issues": [{"category": "Trust", "problem": "no Google reviews embed",
                        "severity": "medium", "evidence": ""}],
            "overall_impression": "Strong copy, missing review widget",
            "lead_score": 8,
        })
        email_resp = json.dumps({
            "subject_line": "quick note about acmeplumbing.example",
            "email_body": "Hi,\\nNoticed your homepage is solid but missing a Google reviews widget.\\nHappy to add it.\\nTomas",
            "follow_up_subject": "re: quick note",
            "follow_up_body": "Bumping this — worth 5 min?\\nTomas",
        })

        with patch.object(analyzer, "_call_llm", side_effect=[analysis, email_resp]):
            result = process_single(
                base + "/", name="Acme Plumbing",
                skip_pagespeed=True, sender_name="Tomas",
            )

        _check("no error", result.get("error") is None)
        _check("seo present", result.get("seo") is not None)
        _check("analysis present", result.get("analysis") is not None)
        _check("email present and has body",
               result.get("email") and result["email"].get("email_body"))
        _check("contact email auto-extracted",
               "owner@acmeplumbing.example" in (result.get("contact_emails") or []))

        # 6. Save → reload → dry-run send
        print("\n6. Output → reload → dry-run send")
        json_path = output.save_json([result], filename="smoke.json")
        _check("json saved", os.path.exists(json_path))
        emails = sender.load_emails_from_audit_json(json_path)
        _check("email loaded back", len(emails) == 1)
        emails[0]["to"] = "test@example.com"
        send_results = sender.send_batch(emails, dry_run=True, delay=0)
        _check("dry run completed", send_results[0]["status"] == "dry_run")

        # 7. Output layer survives messy input
        print("\n7. Output survives mixed/messy results")
        mixed = [
            result,
            {"url": base + "/dead", "error": "fetch failed"},
            {"url": base + "/skip", "skipped_reason": "no_contact_email",
             "analysis": None, "email": None},
        ]
        csv_path = output.save_csv(mixed, filename="smoke.csv")
        _check("csv saved", os.path.exists(csv_path))
        with open(csv_path) as f:
            lines = f.read().splitlines()
        _check("csv has 4 lines (header + 3 rows)", len(lines) == 4)

        # 8. Contract drift guard: send_batch never raises on bad input
        print("\n8. Send batch tolerates malformed entries")
        results = sender.send_batch([
            {},                                            # totally empty
            {"to": "a@b.com", "subject": "x", "body": "y"},
            {"to": None, "subject": "x", "body": "y"},      # None recipient
        ], dry_run=True, delay=0)
        _check("at most one accepted", len(results) <= 1)

        print("\n=== ALL SMOKE CHECKS PASSED ===\n")

    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
