"""HTTP fixture servers that bind numeric loopback addresses without reverse DNS."""

from http.server import HTTPServer
from socketserver import TCPServer, ThreadingMixIn


class LoopbackHTTPServer(HTTPServer):
    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class ThreadingLoopbackHTTPServer(ThreadingMixIn, LoopbackHTTPServer):
    daemon_threads = True
