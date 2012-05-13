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

from binascii import b2a_hex
from copy import deepcopy
from jsonrpcserver import JSONRPCHandler
from time import time
from util import RejectedShare

_NoParams = {}

class _getmemorypool:
	getmemorypool_rv_template = {
		'longpoll': '/LP',
		'mutable': [
			'coinbase/append',
		],
		'noncerange': '00000000ffffffff',
		'target': '00000000ffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
		'version': 1,
	}
	def doJSON_getmemorypool(self, params = _NoParams, sp = _NoParams):
		if isinstance(params, str):
			if sp.get('mode', 'submit') != 'submit':
				raise AttributeError('getmemorypool mode "%s" not supported' % (sp['mode'],))
			rr = self.doJSON_submitblock(params, sp)
			if sp is _NoParams:
				return rr is None
			return rr
		elif not sp is _NoParams:
			raise TypeError('getmemorypool() takes at most 2 positional arguments (%d given)' % (len(a),))
		elif params.get('mode', 'template') != 'template':
			raise AttributeError('getmemorypool mode "%s" not supported' % (sp['mode'],))
		
		if 'longpollid' in params:
			self.processLP(params['longpollid'])
		
		rv = dict(self.getmemorypool_rv_template)
		MC = self.server.getBlockTemplate(self.Username)
		(dummy, merkleTree, cb, prevBlock, bits) = MC
		rv['previousblockhash'] = b2a_hex(prevBlock[::-1]).decode('ascii')
		rv['longpollid'] = str(self.server.LPId)
		tl = []
		for txn in merkleTree.data[1:]:
			tl.append(b2a_hex(txn.data).decode('ascii'))
		rv['transactions'] = tl
		now = int(time())
		rv['time'] = now
		# FIXME: ensure mintime is always >= real mintime, both here and in share acceptance
		rv['mintime'] = now - 180
		rv['maxtime'] = now + 120
		rv['bits'] = b2a_hex(bits[::-1]).decode('ascii')
		t = deepcopy(merkleTree.data[0])
		t.setCoinbase(cb)
		t.assemble()
		rv['coinbasetxn'] = b2a_hex(t.data).decode('ascii')
		return rv
	
	def doJSON_submitblock(self, data, params = _NoParams):
		data = bytes.fromhex(data)
		share = {
			'data': data[:80],
			'blkdata': data[80:],
			'username': self.Username,
			'remoteHost': self.remoteHost,
		}
		try:
			self.server.receiveShare(share)
		except RejectedShare as rej:
			if 'SBB' in self.quirks:
				return False
			return str(rej)
		if 'SBB' in self.quirks:
			return True
		return None

JSONRPCHandler._register(_getmemorypool)
