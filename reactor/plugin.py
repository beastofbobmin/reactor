import http.server
import json
import logging
import socketserver
import ssl
import threading
import time

from reactor.reactor import Reactor
from reactor.util import import_class

logging.getLogger('reactor.plugin').addHandler(logging.NullHandler())


class BasePlugin(object):
    """
    Plugins are threads or processes that run alongside the Reactor core to provide
    additional functionality to the system.
    """

    def __init__(self, reactor: Reactor, conf: dict):
        self.reactor = reactor
        self.conf = conf

    def start(self):
        """ Starts the plugin. """
        raise NotImplementedError()

    def shutdown(self, timeout: int = None):
        """
        Shuts down the plugin.
        :param timeout: Duration in seconds to block waiting for the plugin to shutdown
        """
        raise NotImplementedError()


class HttpServerPlugin(BasePlugin):
    """
    HTTP Server plugin provides a web server with a single endpoint to return basic health check against.
    """
    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        reactor = None

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'ReactorHTTP/1.0'
        sys_version = ''
        error_message_format = '{"code":%(code)d}'
        error_content_type = 'application/json'
        protocol_version = 'HTTP/1.1'

        def _send_json_response(self, code: int, body: str = None):
            self.send_response(code)
            if body is not None:
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', len(body))
                self.send_header('Expires', '-1')
                self.send_header('Cache-Control', 'private, max-age=0')
                self.send_header('X-XSS-Protection', '1; mode=block')
                self.send_header('X-Frame-Options', 'SAMEORIGIN')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

        # noinspection PyPep8Naming
        def do_GET(self):
            if self.path == '/':
                reactor = self.server.reactor  # type: Reactor
                rules = {rule_locator: {} for rule_locator in reactor.loader.keys()}
                for rule_locator in rules:
                    rule = reactor.loader[rule_locator]
                    rules[rule_locator] = {'running': rule_locator in reactor.raft.meta['executing'],
                                           'time_taken': rule.data.time_taken}
                response = {'up_time': time.monotonic(),
                            'cluster': {'size': 1 + len(reactor.raft.neighbours),
                                        'leader': reactor.raft.addr_to_str(reactor.raft.leader),
                                        'neighbourhood': list(map(reactor.raft.addr_to_str, reactor.raft.neighbourhood)),
                                        'changed': time.time() - reactor.raft.changed},
                            'rules': rules}
            else:
                return self.send_error(404, 'Not Found')

            self._send_json_response(200, json.dumps(response).encode('UTF-8'))

    def __init__(self, *args, **kwargs):
        super(HttpServerPlugin, self).__init__(*args, **kwargs)

        self._thread = None  # type: threading.Thread
        self._httpd = None  # type: http.server.HTTPServer
        self._server_class = import_class(self.conf.get('server_class', self.Server))
        self._handler_class = import_class(self.conf.get('handler_class', self.Handler))

    def start(self):
        logging.getLogger('reactor.plugin.http_server').info('Start http server on %d', int(self.conf.get('port', 7100)))
        self._httpd = self._server_class(('', int(self.conf.get('port', 7100))), self._handler_class)
        self._httpd.reactor = self.reactor
        if self.conf.get('ssl'):
            self._httpd.socket = ssl.wrap_socket(self._httpd.socket,
                                                 keyfile=self.conf['ssl'].get('key_file'),
                                                 certfile=self.conf['ssl'].get('crt_file'),
                                                 server_side=True,
                                                 cert_reqs=ssl.CERT_NONE,
                                                 ssl_version=ssl.PROTOCOL_TLSv1_2,
                                                 ca_certs=self.conf['ssl'].get('ca_file'))
        self._thread = threading.Thread(target=self._httpd.serve_forever)
        self._thread.daemon = True
        self._thread.start()

    def shutdown(self, timeout: int = None):
        self._httpd.shutdown()
        self._thread.join(timeout)
        del self._thread
        del self._httpd
