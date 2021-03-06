#
#    CFNS - Rijkswaterstaat CIV, Delft © 2021 - 2022 <cfns@rws.nl>
#
#    Copyright 2021 - 2022 Bastiaan Teeuwen <bastiaan@mkcl.nl>
#
#    This file is part of cap-dab-server
#
#    cap-dab-server is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    cap-dab-server is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with cap-dab-server. If not, see <https://www.gnu.org/licenses/>.
#

import flask                                # Flask HTTP server library
import logging                              # Logging facilities
import logging.handlers                     # Logging handlers
import pyexpat                              # CAP XML parser backend (only used for version check)
import queue                                # Queue for passing data to the DAB processing thread
import re                                   # For removing color from werkzeug's log messages
import threading                            # Threading support (for running Flask in the background)
from werkzeug.serving import make_server    # Flask backend
from cap.parser import CAPParser            # CAP XML parser (internal)
import utils

logger = logging.getLogger('server.cap')

# TODO take a textfile with a list of accepted senders as input

class StripEsc(logging.Filter):
    """
    Werkzeug adds colors to the log file by default.
    Unfortunately, dialog can't display this, so this has to be filtered out.
    """

    def __init__(self):
        # Don't bother with just colors, removing all escape sequences is more straightforward
        self.esc = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]')

    def strip(self, s):
        try:
            return self.esc.sub('', s)
        except:
            return s

    def filter(self, record):
        if record:
            if record.msg:
                record.msg = self.strip(record.msg)
            if type(record.args) is tuple:
                record.args = tuple(map(self.strip, record.args))

        return True

class CAPHTTP(threading.Thread):
    """ Actual Werkzeug/Flask server thread """
    def __init__(self, app, srvcfg):
        threading.Thread.__init__(self)

        host = srvcfg['cap']['host']
        port = int(srvcfg['cap']['port'])

        self.server = make_server(host, port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def join(self):
        self.server.shutdown()
        super().join()

class CAPServer():
    def _index(self):
        # Obtain the Client's IP
        route = flask.request.access_route
        client_addr = next((addr for addr in reversed(route) if addr != '127.0.0.1'), flask.request.remote_addr)

        # Check if Content-Type header is set to an XML MIME type
        content_type = flask.request.content_type

        if not content_type.startswith('application/xml') and not content_type.startswith('text/xml'):
            if utils.logger_strict(logger, self._strict, f'{"FAIL" if self._strict else "WARN"}: invalid Content-Type: {content_type}'):
                return flask.Response(status=415)

        # Initialize the CAP parser
        try:
            cp = CAPParser(self.app, self._strict, self._srvcfg['cap']['identifier'], self._srvcfg['cap']['sender'])
        except Exception as e:
            logger.error(f'Unable to start the CAP parser: {e}')
            return flask.Response(status=500)

        # Parse the Xml into memory and check if all required elements present
        if not cp.parse(flask.request.data):
            logger.error('Unable to parse message')
            return flask.Response(status=400)

        if cp.msg_type == CAPParser.TYPE_LINK_TEST:
            logger.debug(f'{client_addr}: Link Test OK')
            pass
        elif cp.msg_type == CAPParser.TYPE_ALERT:
            logger.debug(f'{client_addr}: Alert OK')
            try:
                self._q.put({
                            'raw': flask.request.data,
                            'msg_type': cp.msg_type,
                            'identifier': cp.identifier,
                            'sender': cp.sender,
                            'sent': cp.sent,
                            'lang': cp.lang,
                            'effective': cp.effective,
                            'expires': cp.expires,
                            'description': cp.description
                            })
            except queue.Full:
                logger.error('Queue is full, perhaps increase queuelimit?')
        elif cp.msg_type == CAPParser.TYPE_CANCEL:
            logger.debug(f'{client_addr}: Alert Cancel OK')

            try:
                self._q.put({
                            'raw': flask.request.data,
                            'msg_type': cp.msg_type,
                            'identifier': cp.identifier,
                            'sender': cp.sender,
                            'sent': cp.sent,
                            'references': cp.references
                            })
            except queue.Full:
                logger.error('Queue is full, perhaps increase queuelimit?')
        else:
            return flask.Response(status=400)

        # Generate an appropriate response
        xml = cp.generate_response(cp.identifier, cp.sender, cp.sent)
        return flask.Response(response=xml, status=200, content_type='application/xml; charset=utf-8')

    def __init__(self, srvcfg, q):
        self.app = flask.Flask(__name__)

        self._srvcfg = srvcfg
        self._q = q

        self._logdir = srvcfg['general']['logdir']
        self._logsize = int(srvcfg['general']['max_log_size']) * 1024
        self._strict = srvcfg['cap'].getboolean('strict_parsing')

        self._cap = None

        # setup the endpoint for '/'
        self.app.add_url_rule('/', 'index', self._index, methods=['POST'])

    def start(self):
        # Check if the version of PyExpat is vulnerable to XML DDoS attacks (version 2.4.1+).
        # See https://docs.python.org/3/library/xml.html#xml-vulnerabilitiesk
        ver = pyexpat.version_info
        if ver[0] < 2 or ver[1] < 4 or ver[2] < 1:
            logger.warn('PyExpat 2.4.1+ is recommended but not found on this system, update your Python installation')

        # Remove Flask and werkzeug's default logging handler(s).
        for h in self.app.logger.handlers:
            self.app.logger.removeHandler(h)
        for h in logging.getLogger('werkzeug').handlers:
            self.app.logger.removeHandler(h)

        # Setup log target
        strip_esc = StripEsc()
        handler = logging.handlers.RotatingFileHandler(f'{self._logdir}/capsrv.log', mode='a', maxBytes=self._logsize, backupCount=5)
        handler.setFormatter(logging.Formatter(fmt='%(asctime)s %(levelname)-8s %(message)s', datefmt='%y-%m-%d %H:%M'))
        handler.setLevel(logging.INFO)
        handler.addFilter(strip_esc)

        # Setup the logging file for werkzeug and Flask
        logging.getLogger('werkzeug').addHandler(handler)
        logging.getLogger('werkzeug').setLevel(logging.INFO)
        self.app.logger.addHandler(handler)
        self.app.logger.setLevel(logging.INFO)

        # Start the werkzeug/Flask thread
        try:
            self._cap = CAPHTTP(self.app, self._srvcfg)
            self._cap.start()
        except KeyError as e:
            logger.error(f'Unable to start CAP HTTP server thread, check configuration. {e}')
            return False
        except Exception as e:
            logger.error(f'Unable to start CAP HTTP server thread. {e}')
            return False

        return True

    def stop(self):
        if self._cap is not None:
            self._cap.join()

    def restart(self):
        self.stop()

        return self.start()

    def status(self):
        return self._cap.is_alive() if self._cap is not None else False
