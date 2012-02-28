# Eloipool - Python Bitcoin pool server
# Copyright (C) 2011-2012  Luke Dashjr <luke-jr+eloipool@utopios.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import httpserver
import json
import logging
import networkserver
import socket
from time import time
import traceback

WithinLongpoll = httpserver.AsyncRequest

class JSONRPCHandler(httpserver.HTTPHandler):
	default_quirks = {
		'NELH': None,  # FIXME: identify which clients have a problem with this
	}
	
	LPHeaders = {
		'X-Long-Polling': None,
	}
	
	logger = logging.getLogger('JSONRPCHandler')
	
	def sendReply(self, status=200, body=b'', headers=None):
		headers = dict(headers) if headers else {}
		if status == 200:
			headers.setdefault('Content-Type', 'application/json')
			headers.setdefault('X-Long-Polling', '/LP')
			headers.setdefault('X-Roll-NTime', 'expire=120')
		elif body and body[0] == 123:  # b'{'
			headers.setdefault('Content-Type', 'application/json')
		return super().sendReply(status, body, headers)
	
	def doError(self, reason = '', code = 100):
		reason = json.dumps(reason)
		reason = r'{"result":null,"id":null,"error":{"name":"JSONRPCError","code":%d,"message":%s}}' % (code, reason)
		return self.sendReply(500, reason.encode('utf8'))
	
	def checkAuthentication(self, un, pw):
		return bool(un)
	
	def doHeader_user_agent(self, value):
		self.reqinfo['UA'] = value
		quirks = self.quirks
		try:
			if value[:9] == b'phoenix/v':
				v = tuple(map(int, value[9:].split(b'.')))
				if v[0] < 2 and v[1] < 8 and v[2] < 1:
					quirks['NELH'] = None
		except:
			pass
		self.quirks = quirks
	
	def doHeader_x_minimum_wait(self, value):
		self.reqinfo['MinWait'] = int(value)
	
	def doHeader_x_mining_extensions(self, value):
		self.extensions = value.decode('ascii').lower().split(' ')
	
	def doLongpoll(self):
		timeNow = time()
		
		self._LP = True
		if 'NELH' not in self.quirks:
			# [NOT No] Early Longpoll Headers
			self.sendReply(200, body=None, headers=self.LPHeaders)
			self.push(b"1\r\n{\r\n")
			self.changeTask(self._chunkedKA, timeNow + 45)
		else:
			self.changeTask(None)
		
		waitTime = self.reqinfo.get('MinWait', 15)  # TODO: make default configurable
		self.waitTime = waitTime + timeNow
		
		totfromme = self.LPTrack()
		self.server._LPClients[id(self)] = self
		self.logger.debug("New LP client; %d total; %d from %s" % (len(self.server._LPClients), totfromme, self.addr[0]))
		
		raise WithinLongpoll
	
	def _chunkedKA(self):
		# Keepalive via chunked transfer encoding
		self.push(b"1\r\n \r\n")
		self.changeTask(self._chunkedKA, time() + 45)
	
	def LPTrack(self):
		myip = self.addr[0]
		if myip not in self.server.LPTracking:
			self.server.LPTracking[myip] = 0
		self.server.LPTracking[myip] += 1
		return self.server.LPTracking[myip]
	
	def LPUntrack(self):
		self.server.LPTracking[self.addr[0]] -= 1
	
	def cleanupLP(self):
		# Called when the connection is closed
		if not self._LP:
			return
		self.changeTask(None)
		try:
			del self.server._LPClients[id(self)]
		except KeyError:
			pass
		self.LPUntrack()
	
	def wakeLongpoll(self):
		now = time()
		if now < self.waitTime:
			self.changeTask(self.wakeLongpoll, self.waitTime)
			return
		else:
			self.changeTask(None)
		
		self.LPUntrack()
		
		rv = self.doJSON_getwork()
		rv['submitold'] = True
		rv = {'id': 1, 'error': None, 'result': rv}
		rv = json.dumps(rv)
		rv = rv.encode('utf8')
		if 'NELH' not in self.quirks:
			rv = rv[1:]  # strip the '{' we already sent
			self.push(('%x' % len(rv)).encode('utf8') + b"\r\n" + rv + b"\r\n0\r\n\r\n")
		else:
			self.sendReply(200, body=rv, headers=self.LPHeaders)
		
		self.reset_request()
	
	def doJSON(self, data):
		# TODO: handle JSON errors
		data = data.decode('utf8')
		try:
			data = json.loads(data)
			method = 'doJSON_' + str(data['method']).lower()
		except ValueError:
			return self.doError(r'Parse error')
		except TypeError:
			return self.doError(r'Bad call')
		if not hasattr(self, method):
			return self.doError(r'Procedure not found')
		# TODO: handle errors as JSON-RPC
		self._JSONHeaders = {}
		params = data.setdefault('params', ())
		try:
			rv = getattr(self, method)(*tuple(data['params']))
		except Exception as e:
			self.logger.error(("Error during JSON-RPC call: %s%s\n" % (method, params)) + traceback.format_exc())
			return self.doError(r'Service error: %s' % (e,))
		if rv is None:
			# response was already sent (eg, authentication request)
			return
		rv = {'id': data['id'], 'error': None, 'result': rv}
		try:
			rv = json.dumps(rv)
		except:
			return self.doError(r'Error encoding reply in JSON')
		rv = rv.encode('utf8')
		return self.sendReply(200, rv, headers=self._JSONHeaders)
	
	def handle_close(self):
		self.cleanupLP()
		super().handle_close()
	
	def handle_request(self):
		if not self.Username:
			return self.doAuthenticate()
		if not self.method in (b'GET', b'POST'):
			return self.sendReply(405)
		if not self.path in (b'/', b'/LP', b'/LP/'):
			return self.sendReply(404)
		try:
			if self.path[:3] == b'/LP':
				return self.doLongpoll()
			data = b''.join(self.incoming)
			return self.doJSON(data)
		except socket.error:
			raise
		except WithinLongpoll:
			raise
		except:
			self.logger.error(traceback.format_exc())
			return self.doError('uncaught error')
	
	def reset_request(self):
		self._LP = False
		super().reset_request()
	
setattr(JSONRPCHandler, 'doHeader_user-agent', JSONRPCHandler.doHeader_user_agent);
setattr(JSONRPCHandler, 'doHeader_x-minimum-wait', JSONRPCHandler.doHeader_x_minimum_wait);
setattr(JSONRPCHandler, 'doHeader_x-mining-extensions', JSONRPCHandler.doHeader_x_mining_extensions);

JSONRPCListener = networkserver.NetworkListener

class JSONRPCServer(networkserver.AsyncSocketServer):
	logger = logging.getLogger('JSONRPCServer')
	
	waker = True
	
	def __init__(self, *a, **ka):
		ka.setdefault('RequestHandlerClass', JSONRPCHandler)
		super().__init__(*a, **ka)
		
		self.SecretUser = None
		
		self.LPRequest = False
		self._LPClients = {}
		self._LPWaitTime = time() + 15
		
		self.LPTracking = {}
	
	def pre_schedule(self):
		if self.LPRequest == 1:
			self._LPsch()
	
	def wakeLongpoll(self):
		if self.LPRequest:
			self.logger.info('Ignoring longpoll attempt while another is waiting')
			return
		self.LPRequest = 1
		self.wakeup()
	
	def _LPsch(self):
		now = time()
		if self._LPWaitTime > now:
			delay = self._LPWaitTime - now
			self.logger.info('Waiting %.3g seconds to longpoll' % (delay,))
			self.schedule(self._actualLP, self._LPWaitTime)
			self.LPRequest = 2
		else:
			self._actualLP()
	
	def _actualLP(self):
		self.LPRequest = False
		C = tuple(self._LPClients.values())
		self._LPClients = {}
		if not C:
			self.logger.info('Nobody to longpoll')
			return
		OC = len(C)
		self.logger.debug("%d clients to wake up..." % (OC,))
		
		now = time()
		
		for ic in C:
			try:
				ic.wakeLongpoll()
			except socket.error:
				OC -= 1
				# Ignore socket errors; let the main event loop take care of them later
			except:
				OC -= 1
				self.logger.debug('Error waking longpoll handler:\n' + traceback.format_exc())
		
		self._LPWaitTime = time()
		self.logger.info('Longpoll woke up %d clients in %.3f seconds' % (OC, self._LPWaitTime - now))
		self._LPWaitTime += 5  # TODO: make configurable: minimum time between longpolls
	
	def TopLPers(self, n = 0x10):
		tmp = list(self.LPTracking.keys())
		tmp.sort(key=lambda k: self.LPTracking[k])
		for jerk in map(lambda k: (k, self.LPTracking[k]), tmp[-n:]):
			print(jerk)
