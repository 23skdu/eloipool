from base64 import b64decode
from binascii import b2a_hex
from datetime import datetime
from email.utils import formatdate
import json
import select
import socketserver
from time import mktime
import traceback

# TODO: keepalive/close
_CheckForDupesHACK = {}
class JSONRPCHandler(socketserver.StreamRequestHandler):
	HTTPStatus = {
		200: 'OK',
		401: 'Unauthorized',
		404: 'Not Found',
		405: 'Method Not Allowed',
		500: 'Internal Server Error',
	}
	
	def sendReply(self, status=200, body=b'', headers=None):
		wfile = self.wfile
		buf = "HTTP/1.1 %d Eligius\n" % (status,)
		headers = dict(headers) if headers else {}
		headers['Date'] = formatdate(timeval=mktime(datetime.now().timetuple()), localtime=False, usegmt=True)
		if body is None:
			headers.setdefault('Transfer-Encoding', 'chunked')
			body = b''
		else:
			headers['Content-Length'] = len(body)
		if status == 200:
			headers.setdefault('Content-Type', 'application/json')
			#headers.setdefault('X-Long-Polling', '/LP')
		for k, v in headers.items():
			if v is None: continue
			buf += "%s: %s\n" % (k, v)
		buf += "\n"
		buf = buf.encode('utf8')
		buf += body
		wfile.write(buf)
	
	def doError(self, reason = ''):
		return self.sendReply(500, reason.encode('utf8'))
	
	def doHeader_authorization(self, value):
		value = value.split(b' ')
		if len(value) != 2 or value[0] != b'Basic':
			return self.doError('Bad Authorization header')
		value = b64decode(value[1])
		value = value.split(b':')[0]
		self.Username = value
	
	def doHeader_content_length(self, value):
		self.CL = int(value)
	
	def doAuthenticate(self):
		self.sendReply(401, headers={'WWW-Authenticate': 'Basic realm="Eligius"'})
	
	def doLongpoll(self):
		pass # TODO
	
	def doJSON(self, data):
		# TODO: handle JSON errors
		data = data.decode('utf8')
		data = json.loads(data)
		method = 'doJSON_' + str(data['method']).lower()
		if not hasattr(self, method):
			return self.doError('No such method')
		# TODO: handle errors as JSON-RPC
		rv = getattr(self, method)(*tuple(data['params']))
		rv = {'id': data['id'], 'error': None, 'result': rv}
		rv = json.dumps(rv)
		rv = rv.encode('utf8')
		return self.sendReply(200, rv)
	
	getwork_rv_template = {
		'target': 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffff00000000',
		'hash1': '00000000000000000000000000000000000000000000000000000000000000000000008000000000000000000000000000000000000000000000000000010000',
	}
	def doJSON_getwork(self, data=None):
		if not data is None:
			return self.doJSON_submitwork(data)
		rv = dict(self.getwork_rv_template)
		(hdr, MRD) = self.server.getBlockHeader()
		
		# FIXME: this assumption breaks with noncerange or pool-side rollntime
		global _CheckForDupesHACK
		if hdr in _CheckForDupesHACK:
			raise self.server.RaiseRedFlags(RuntimeError('issuing duplicate work'))
		_CheckForDupesHACK[hdr] = None
		
		data = b2a_hex(hdr).decode('utf8')
		# TODO: endian shuffle etc
		rv['data'] = data
		# TODO: rv['midstate'] = 
		return rv
	
	def doJSON_submitwork(self, data):
		return 'TODO'  # TODO
		pass
	
	def handle(self):
		# TODO: handle socket errors
		rfile = self.rfile
		data = rfile.readline().strip()
		data = data.split(b' ')
		if not data[0] in (b'GET', b'POST'):
			return self.sendReply(405)
		path = data[1]
		if not path in (b'/', b'/LP'):
			return self.sendReply(404)
		self.CL = None
		self.Username = None
		while True:
			data = rfile.readline().strip()
			if not data:
				break
			data = tuple(map(lambda a: a.strip(), data.split(b':', 1)))
			method = 'doHeader_' + data[0].decode('ascii').lower()
			if hasattr(self, method):
				getattr(self, method)(data[1])
		if not self.Username:
			return self.doAuthenticate()
		data = rfile.read(self.CL) if self.CL else None
		try:
			if path == b'/LP':
				return self.doLongpoll()
			return self.doJSON(data)
		except:
			print(traceback.format_exc())
			return self.doError('uncaught error')
setattr(JSONRPCHandler, 'doHeader_content-length', JSONRPCHandler.doHeader_content_length);

class JSONRPCServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
	allow_reuse_address = True
	
	def __init__(self, server_address, RequestHandlerClass=JSONRPCHandler, *a, **k):
		super().__init__(server_address, RequestHandlerClass, *a, **k)
	
	def serve_forever(self, *a, **k):
		while True:
			try:
				super().serve_forever(*a, **k)
			except select.error:
				pass
