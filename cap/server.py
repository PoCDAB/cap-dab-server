import threading                            # Threading support (for running Flask in the background)
import logging                              # Logging facilities
import pyexpat                              # CAP XML parser backend (only used for version check)
import os                                   # For redirecting Flask's logging output to a file using an env. variable
from flask import Flask, Response, request  # Flask HTTP server library
from werkzeug.serving import make_server    # Flask backend
from cap.parser import CAPParser            # CAP XML parser (internal)
from cap.parser import logging_strict       # More logging facilities

app = Flask(__name__)
cp = None               # CAP XML parser

# Main HTTP POST request handler
@app.post('/')
def index():
    content_type = request.content_type

    # Check if Content-Type header is set to an XML MIME type
    if not content_type.startswith('application/xml') and not content_type.startswith('text/xml'):
        if logging_strict(f'{"FAIL" if strict else "WARN"}: invalid Content-Type: {content_type}'):
            return Response(status=415)

    # Initialize the CAP parser
    try:
        cp = CAPParser(strict)
    except Exception as e:
        logging.error(f'FAIL: {e}')
        exit(1)

    # parse the Xml into memory and check if all required elements present
    if not cp.parse(request.data):
        return Response(status=400)

    # Generate an appropriate response
    xml = cp.generate_response()
    return Response(response=xml, status=200, content_type='application/xml; charset=utf-8')

class CAPServer(threading.Thread):
    def __init__(self, app, host, port):
        threading.Thread.__init__(self)

        self.server = make_server(host, port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def join(self):
        print('Waiting for CAP HTTP server to terminate...', end='')
        self.server.shutdown()
        print('OK')

def cap_server(host, port, strict_parsing):
    global strict
    strict = strict_parsing

    # Check if the version of PyExpat is vulnerable to XML DDoS attacks (version 2.4.1+).
    # See https://docs.python.org/3/library/xml.html#xml-vulnerabilitiesk
    ver = pyexpat.version_info
    if ver[0] < 2 or ver[1] < 4 or ver[2] < 1:
        raise ModuleNotFoundError('PyExpat 2.4.1+ is required but not found on this system')

    print('Starting up CAP HTTP server...')

    os.environ['WERKZEUG_RUN_MAIN'] = 'true'

    server = CAPServer(app, host, port)
    server.start()

    return server