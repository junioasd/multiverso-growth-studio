"""Regression: slow request must not block API liveness in a separate connection.
Pure loopback, ephemeral port, no production DB and no external side effects.
"""
import threading,time,urllib.request,unittest
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch
from api import server as api_server

class LivenessConcurrencyTest(unittest.TestCase):
    def test_liveness_not_blocked_by_slow_handler(self):
        entered = threading.Event()
        release = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/slow":
                    entered.set()
                    release.wait(timeout=3)
                self.send_response(200)
                self.end_headers()
                try: self.wfile.write(b"ok")
                except (BrokenPipeError,ConnectionResetError): pass
            def log_message(self,*args): pass
        with patch.object(api_server,"make_handler",return_value=Handler):
            srv=api_server.run_server(host="127.0.0.1",port=0,app=object())
        base=f"http://127.0.0.1:{srv.server_address[1]}"
        t=threading.Thread(target=srv.serve_forever,daemon=True)
        t.start()
        failures=[]
        def slow():
            try:
                with urllib.request.urlopen(base+"/slow",timeout=4) as response: response.read()
            except Exception as e: failures.append(type(e).__name__)
        slow_thread=threading.Thread(target=slow,daemon=True)
        slow_thread.start()
        try:
            self.assertTrue(entered.wait(timeout=1.5),"slow endpoint did not start")
            begin=time.monotonic()
            with urllib.request.urlopen(base+"/api/health",timeout=0.9) as res:
                self.assertEqual(res.status,200)
                self.assertEqual(res.read(),b"ok")
            self.assertLess(time.monotonic()-begin,0.9)
        finally:
            release.set()
            slow_thread.join(timeout=4)
            srv.shutdown()
            srv.server_close()
            t.join(timeout=3)

if __name__=="__main__": unittest.main(verbosity=2)
