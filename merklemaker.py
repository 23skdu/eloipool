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
from bitcoin.script import countSigOps
from bitcoin.txn import Txn
from collections import deque
from queue import Queue
import jsonrpc
import logging
from math import log
from merkletree import MerkleTree
from struct import pack
import threading
from time import sleep, time
import traceback
from util import BEhash2int, Bits2Target

_makeCoinbase = [0, 0]
inf = float('inf')

class merkleMaker(threading.Thread):
	OldGMP = None
	GMPReq = {
		'capabilities': [
			'coinbasevalue',
			'coinbase/append',
			'coinbase',
			'generation',
			'time',
			'transactions/remove',
			'prevblock',
		],
		'tx': 'obj',
	}
	
	def __init__(self, *a, **k):
		super().__init__(*a, **k)
		self.daemon = True
		self.logger = logging.getLogger('merkleMaker')
		self.CoinbasePrefix = b''
		self.CoinbaseAux = {}
		self.isOverflowed = False
		self.lastWarning = {}
		self.MinimumTxnUpdateWait = 5
		self.overflowed = 0
	
	def _prepare(self):
		self.access = jsonrpc.ServiceProxy(self.UpstreamURI)
		
		self.currentBlock = (None, None)
		self.currentMerkleTree = None
		self.merkleRoots = deque(maxlen=self.WorkQueueSizeRegular[1])
		self.LowestMerkleRoots = self.WorkQueueSizeRegular[1]
		self.clearMerkleTree = MerkleTree([self.clearCoinbaseTxn])
		self.clearMerkleTree.upstreamTarget = (2 ** 224) - 1
		self.clearMerkleTree.coinbasePrefix = b''
		self.clearMerkleTree.timeOffset = 0
		self.clearMerkleRoots = Queue(max(self.WorkQueueSizeLongpoll[1], 1))
		self.LowestClearMerkleRoots = self.WorkQueueSizeLongpoll[1]
		
		if not hasattr(self, 'WarningDelay'):
			self.WarningDelay = max(15, self.MinimumTxnUpdateWait * 2)
		if not hasattr(self, 'WarningDelayTxnLongpoll'):
			self.WarningDelayTxnLongpoll = self.WarningDelay
		if not hasattr(self, 'WarningDelayMerkleUpdate'):
			self.WarningDelayMerkleUpdate = self.WarningDelay
		
		self.lastMerkleUpdate = 0
		self.nextMerkleUpdate = 0
		global now
		now = time()
		self.updateMerkleTree()
	
	def updateBlock(self, newBlock, bits = None, _HBH = None):
		if newBlock == self.currentBlock[0]:
			if bits in (None, self.currentBlock[1]):
				return
			self.logger.error('Was working on block with wrong specs: %s (bits: %s->%s)' % (
				b2a_hex(newBlock[::-1]).decode('utf8'),
				b2a_hex(self.currentBlock[1][::-1]).decode('utf8'),
				b2a_hex(bits[::-1]).decode('utf8'),
			))
		
		if bits is None:
			bits = self.currentBlock[1]
		if _HBH is None:
			_HBH = (b2a_hex(newBlock[::-1]).decode('utf8'), b2a_hex(bits[::-1]).decode('utf8'))
		self.logger.info('New block: %s (bits: %s)' % _HBH)
		self.merkleRoots.clear()
		self.currentMerkleTree = self.clearMerkleTree
		if self.currentBlock[0] != newBlock:
			self.lastBlock = self.currentBlock
		self.currentBlock = (newBlock, bits)
		self.clearMerkleTree.upstreamTarget = max(self.clearMerkleTree.upstreamTarget, Bits2Target(bits))
		self.needMerkle = 2
		self.onBlockChange()
	
	def _trimBlock(self, MP, txnlist, txninfo, floodn, msgf):
		if 'transactions' not in MP['mutable'] and 'transactions/remove' not in MP['mutable']:
			raise self._floodCritical(now, floodn, doin=msgf('transactions not mutable'))
		fee = txninfo[-1].get('fee', None)
		if fee is None:
			raise self._floodCritical(now, floodn, doin=msgf('fees unknown'))
		if fee:
			if 'generation' not in MP['mutable'] and 'coinbasetxn' in MP:
				raise self._floodCritical(now, floodn, doin=msgf('generation not mutable'))
			# FIXME: coinbasevalue is *not* guaranteed to exist here
			MP['coinbasevalue'] -= fee
		
		txnlist[-1:] = ()
		txninfo[-1:] = ()
		
		return True
	
	def _APOT(self, txninfopot, MP, POTInfo):
		feeTxnsTrimmed = 0
		feesTrimmed = 0
		for txn in txninfopot:
			if txn.get('fee') is None:
				self._floodWarning(now, 'APOT-No-Fees', doin='Upstream didn\'t provide fee information required for aggressive POT', logf=self.logger.info)
				return
			if not txn['fee']:
				continue
			feesTrimmed += txn['fee']
			feeTxnsTrimmed += 1
		MP['coinbasevalue'] -= feesTrimmed
		
		POTInfo[2] = [feeTxnsTrimmed, feesTrimmed]
		self._floodWarning(now, 'POT-Trimming-Fees', doin='Aggressive POT trimming %d transactions with %d.%08d BTC total fees' % (feeTxnsTrimmed, feesTrimmed//100000000, feesTrimmed % 100000000), logf=self.logger.debug)
		
		return True
	
	def _makeBlockSafe(self, MP, txnlist, txninfo):
		blocksize = sum(map(len, txnlist)) + 80
		while blocksize > 934464:  # 1 "MB" limit - 64 KB breathing room
			txnsize = len(txnlist[-1])
			self._trimBlock(MP, txnlist, txninfo, 'SizeLimit', lambda x: 'Making blocks over 1 MB size limit (%d bytes; %s)' % (blocksize, x))
			blocksize -= txnsize
		
		# NOTE: This check doesn't work at all without BIP22 transaction obj format
		blocksigops = sum(a.get('sigops', 0) for a in txninfo)
		while blocksigops > 19488:  # 20k limit - 0x200 breathing room
			txnsigops = txninfo[-1]['sigops']
			self._trimBlock(MP, txnlist, txninfo, 'SigOpLimit', lambda x: 'Making blocks over 20k SigOp limit (%d; %s)' % (blocksigops, x))
			blocksigops -= txnsigops
		
		POTMode = getattr(self, 'POT', 1)
		txncount = len(txnlist) + 1
		if POTMode and 'transactions' not in MP['mutable'] and 'transactions/remove' not in MP['mutable']:
			POTMode = 0
			if txncount != 2**int(log(txncount, 2)):
				self._floodWarning(now, 'No-POT', doin='Upstream does not allow transactions/remove mutation, required for POT mode')
		if POTMode > 1 and 'generation' not in MP['mutable'] and 'coinbasetxn' in MP:
			POTMode = 1
			if txncount != 2**int(log(txncount, 2)):
				self._floodWarning(now, 'No-Aggressive-POT', doin='Upstream does not allow generation mutation, required for aggressive POT mode')
		if POTMode:
			feetxncount = txncount
			for i in range(txncount - 2, -1, -1):
				if 'fee' not in txninfo[i] or txninfo[i]['fee']:
					break
				feetxncount -= 1
			
			if getattr(self, 'Greedy', None):
				# Aim to cut off extra zero-fee transactions on the end
				# NOTE: not cutting out ones intermixed, in case of dependencies
				idealtxncount = feetxncount
			else:
				idealtxncount = txncount
			
			pot = 2**int(log(idealtxncount, 2))
			POTInfo = MP['POTInfo'] = [[idealtxncount, feetxncount, txncount], [pot, None], None]
			if pot < idealtxncount:
				if pot * 2 <= txncount:
					pot *= 2
				elif pot >= feetxncount:
					pass
				elif POTMode > 1 and self._APOT(txninfo[pot-1:], MP, POTInfo):
					# Trimmed even transactions with fees
					pass
				else:
					pot = idealtxncount
					self._floodWarning(now, 'Non-POT', doin='Making merkle tree with %d transactions (ideal: %d; max: %d)' % (pot, idealtxncount, txncount))
			POTInfo[1][1] = pot
			pot -= 1
			txnlist[pot:] = ()
			txninfo[pot:] = ()
	
	# This is quite long, but basically it just defines the time limits for a job...
	def _figureTimeRules(self, MP, newMerkleTree):
		global now
		
		intnow = int(now)
		if 'expires' in MP:
			expire = intnow + MP['expires'] - 10
		else:
			# This way, we won't bother updating the merkle tree too often and won't "run out" of work
			expire = intnow
			expire -= expire % 60
			expire += 120
		newMerkleTree.jobExpire = expire
		
		if 'curtime' not in MP: MP['curtime'] = intnow
		stime = MP['curtime']
		timeOffset = newMerkleTree.timeOffset = stime - intnow
		sexpire = expire + timeOffset
		if abs(timeOffset) > 300:
			W = 'Time difference between upstream and local over a minute (%d seconds)' % (timeOffset,)
			self._floodWarning(now, 'TimeOffset', lambda: W, W)
		
		if 'maxtimeoff' in MP:
			# Relative maximum time
			maxtimeOffset = MP['maxtimeoff']
			if maxtimeOffset < 0:
				raise self._floodCritical(now, 'maxtimeoff < 0', doin='Upstream maxtimeoff is negative; this is not supported!')
			if maxtimeOffset > sexpire + 7200:
				# We'd never go allow this new anyway, so make comparison easier
				maxtimeOffset = 7200
		else:
			maxtimeOffset = 7200
		newMerkleTree.maxtimeOffset = maxtimeOffset
		if 'maxtime' in MP:
			# Absolute maximum time
			maxtime = MP['maxtime']
			if maxtime > sexpire + maxtimeOffset:
				# We'd never allow this new anyway, so make comparison easier
				newMerkleTree.maxtime = inf
			elif maxtime < stime:
				raise self._floodCritical(now, 'maxtime < curtime', doin='Upstream maxtime is before curtime; this is not supported!')
			else:
				newMerkleTree.maxtime = maxtime
		else:
			newMerkleTree.maxtime = inf
		if 'mintimeoff' in MP:
			# Relative minimum time
			mintimeOffset = MP['mintimeoff']
			if mintimeOffset <= -300:
				# We'd never allow this old anyway, so make comparison easier
				mintimeOffset = -300
			elif mintimeOffset > 0:
				raise self._floodCritical(now, 'mintimeoff > 0', doin='Upstream mintimeoff is positive; this is not supported!')
			else:
				mintimeOffset = mintimeOffset
		else:
			mintimeOffset = -300
		newMerkleTree.mintimeOffset = mintimeOffset
		if 'mintime' in MP:
			# Absolute minimum time
			mintime = MP['mintime']
			if mintime <= stime + mintimeOffset:
				# We'd never allow this old anyway, so make comparison easier
				newMerkleTree.mintime = 0
			elif mintime > stime:
				raise self._floodCritical(now, 'mintime > curtime', doin='Upstream mintime is after curtime; this is not supported!')
			else:
				newMerkleTree.mintime = mintime
		else:
			# Undefined. Use curtime.
			newMerkleTree.mintime = stime
	
	def updateMerkleTree(self):
		global now
		self.logger.debug('Polling bitcoind for memorypool')
		self.nextMerkleUpdate = now + self.TxnUpdateRetryWait
		
		try:
			MP = self.access.getmemorypool(self.GMPReq)
			self.OldGMP = False
			oMP = None
		except:
			MP = False
			try:
				oMP = self.access.getmemorypool()
			except:
				oMP = False
			if oMP is False:
				# This way, we get the error from the BIP22 call if the old one fails too
				raise
		if MP is False:
			# Pre-BIP22 server (bitcoind <0.7 or Eloipool <20120513)
			if not self.OldGMP:
				self.OldGMP = True
				self.logger.warning('Upstream server is not BIP 22 compliant')
			MP = oMP or self.access.getmemorypool()
		
		
		if 'coinbaseaux' in MP:
			for k, v in MP['coinbaseaux'].items():
				self.CoinbaseAux[k] = bytes.fromhex(v)
		
		if 'noncerange' in MP and MP['noncerange'] != '00000000ffffffff':
			self.logger.critical('Upstream has restricted noncerange; this is not supported!')
			raise RuntimeError('noncerange restricted')
		
		prevBlock = bytes.fromhex(MP['previousblockhash'])[::-1]
		bits = bytes.fromhex(MP['bits'])[::-1]
		if (prevBlock, bits) != self.currentBlock:
			self.updateBlock(prevBlock, bits, _HBH=(MP['previousblockhash'], MP['bits']))
		
		txnlist = MP['transactions']
		if len(txnlist) and isinstance(txnlist[0], dict):
			txninfo = txnlist
			txnlist = tuple(a['data'] for a in txnlist)
		elif 'transactionfees' in MP:
			# Backward compatibility with pre-BIP22 gmp_fees branch
			txninfo = [{'fee':a} for a in MP['transactionfees']]
		else:
			# Backward compatibility with pre-BIP22 hex-only (bitcoind <0.7, Eloipool <future)
			txninfo = [{}] * len(txnlist)
		# TODO: cache Txn or at least txid from previous merkle roots?
		txnlist = [a for a in map(bytes.fromhex, txnlist)]
		
		if 'mutable' not in MP:
			if 'time' in MP:
				# Pre-BIP22, assume bitcoind
				MP['mutable'] = ('time', 'transactions', 'prevblock')
			else:
				MP['mutable'] = ()
		mutable = MP['mutable']
		
		self._makeBlockSafe(MP, txnlist, txninfo)
		
		if 'coinbasetxn' in MP:
			tmpltxn = None
			if 'generation' in mutable:
				if 'coinbasevalue' in MP:
					cbval = MP['coinbasevalue']
				else:
					# Add up the outputs from coinbasetxn
					tmpltxn = Txn(MP['coinbasetxn'])
					tmpltxn.disassemble()
					cbval = 0
					for tmploutput in tmpltxn.outputs:
						cbval += tmploutput[0]
				cbtxn = self.makeCoinbaseTxn(cbval)
			else:
				cbtxn = Txn(bytes.fromhex(MP['coinbasetxn']))
				cbtxn.disassemble()
			if 'coinbase' in mutable:
				# Any coinbase we want
				cbpfx = b''
			elif 'coinbase/append' in mutable:
				if 'generation' in mutable:
					if tmpltxn is None:
						tmpltxn = Txn(MP['coinbasetxn'])
						tmpltxn.disassemble()
					tmplcb = tmpltxn.getCoinbase()
					if self.CoinbasePrefix in tmplcb:
						if not self.CoinbasePrefix:
							self.logger.critical('Upstream requires coinbase prefix; need a unique CoinbasePrefix configued to cope')
						elif len(self.CoinbasePrefix) < 4:
							self.logger.error('CoinbasePrefix appeared in upstream mandatory coinbase data; try making it longer')
						else:
							self.logger.error('CoinbasePrefix appeared in upstream mandatory coinbase data; better luck next time?')
						raise RuntimeError('upstream coinbase contained my prefix')
					cbpfx = tmplcb
				else:
					cbpfx = cbtxn.getCoinbase()
			else:
				# Can't change the coinbase data, but we can abuse a generation...
				# TODO
				self.logger.critical('Upstream does not allow modifying coinbase data; this is not supported!')
				raise RuntimeError
		else:
			cbtxn = self.makeCoinbaseTxn(MP['coinbasevalue'])
			cbpfx = b''
		cbtxn.setCoinbase(b'\0\0')
		cbtxn.assemble()
		txnlist.insert(0, cbtxn.data)
		txninfo.insert(0, {})
		
		txnlist = [a for a in map(Txn, txnlist[1:])]
		txnlist.insert(0, cbtxn)
		txnlist = list(txnlist)
		newMerkleTree = MerkleTree(txnlist)
		newMerkleTree.coinbasePrefix = cbpfx
		
		if 'target' in MP:
			newMerkleTree.upstreamTarget = BEhash2int(bytes.fromhex(MP['target']))
		else:
			newMerkleTree.upstreamTarget = Bits2Target(bits)
		
		self._figureTimeRules(MP, newMerkleTree)
		
		haveUpdate = newMerkleTree.merkleRoot() != self.currentMerkleTree.merkleRoot()
		for k in ('upstreamTarget', 'coinbasePrefix', 'timeOffset', 'mintime', 'mintimeOffset', 'maxtime', 'maxtimeOffset'):
			v = getattr(newMerkleTree, k, None)
			if v == getattr(self.currentMerkleTree, k, None):
				continue
			haveUpdate = True
			if v is None:
				delattr(self.clearMerkleTree, k)
			else:
				setattr(self.clearMerkleTree, k, v)
		
		if haveUpdate:
			newMerkleTree.POTInfo = MP.get('POTInfo')
			self.logger.debug('Updating merkle tree')
			self.currentMerkleTree = newMerkleTree
		self.lastMerkleUpdate = now
		self.nextMerkleUpdate = now + self.MinimumTxnUpdateWait
		noLaterThan = newMerkleTree.jobExpire - getattr(self, 'ExpectedUpstreamLatency', 0) - getattr(self, 'MinimumJobExpiration', 64)
		self.nextMerkleUpdate = min(self.nextMerkleUpdate, noLaterThan)
		
		if self.needMerkle == 2:
			self.needMerkle = 1
			self.needMerkleSince = now
	
	def makeCoinbase(self, pfx = b''):
		now = int(time())
		if now > _makeCoinbase[0]:
			_makeCoinbase[0] = now
			_makeCoinbase[1] = 0
		else:
			_makeCoinbase[1] += 1
		rv = pack('>L', now) + pack('>Q', _makeCoinbase[1]).lstrip(b'\0')
		# NOTE: Not using varlenEncode, since this is always guaranteed to be < 100
		rv = bytes( (len(rv),) ) + rv
		rv = pfx + self.CoinbasePrefix + rv
		for v in self.CoinbaseAux.values():
			if v not in pfx:
				rv += v
		if len(rv) > 100:
			t = time()
			if self.overflowed < t - 300:
				self.logger.warning('Overflowing coinbase data! %d bytes long' % (len(rv),))
				self.overflowed = t
				self.isOverflowed = True
			rv = rv[:100]
		else:
			self.isOverflowed = False
		return rv
	
	def makeMerkleRoot(self, merkleTree):
		cbtxn = merkleTree.data[0]
		cbpfx = merkleTree.coinbasePrefix
		cb = self.makeCoinbase(cbpfx)
		cbtxn.setCoinbase(cb)
		cbtxn.assemble()
		merkleRoot = merkleTree.merkleRoot()
		return (merkleRoot, merkleTree, cb)
	
	_doing_last = None
	def _doing(self, what):
		if self._doing_last == what:
			self._doing_i += 1
			return
		global now
		if self._doing_last:
			self.logger.debug("Switching from (%4dx in %5.3f seconds) %s => %s" % (self._doing_i, now - self._doing_s, self._doing_last, what))
		self._doing_last = what
		self._doing_i = 1
		self._doing_s = now
	
	def _floodWarning(self, now, wid, wmsgf = None, doin = True, logf = None):
		if doin is True:
			doin = self._doing_last
			def a(f = wmsgf):
				return lambda: "%s (doing %s)" % (f(), doin)
			wmsgf = a()
		winfo = self.lastWarning.setdefault(wid, [0, None])
		(lastTime, lastDoing) = winfo
		if now <= lastTime + max(5, self.MinimumTxnUpdateWait) and doin == lastDoing:
			return
		winfo[0] = now
		nowDoing = doin
		winfo[1] = nowDoing
		if logf is None:
			logf = self.logger.warning
		logf(wmsgf() if wmsgf else doin)
	
	def _floodCritical(self, now, wid, wmsgf = None, doin = True):
		self._floodWarning(now, wid, wmsgf, doin, self.logger.critical)
		return RuntimeError(wid)
	
	def merkleMaker_I(self):
		global now
		
		# First, update merkle tree if we haven't for a while and aren't crunched for time
		now = time()
		if self.nextMerkleUpdate <= now and self.clearMerkleRoots.qsize() >= self.WorkQueueSizeLongpoll[0] and len(self.merkleRoots) >= self.WorkQueueSizeRegular[0]:
			self.updateMerkleTree()
		# Next, fill up the longpoll queue first, since it can be used as a failover for the main queue
		elif self.clearMerkleRoots.qsize() < self.WorkQueueSizeLongpoll[1]:
			self._doing('blank merkle roots')
			self.clearMerkleRoots.put(self.makeMerkleRoot(self.clearMerkleTree))
		# Next, fill up the main queue (until they're all current)
		elif len(self.merkleRoots) < self.WorkQueueSizeRegular[1] or self.merkleRoots[0][1] != self.currentMerkleTree:
			if self.needMerkle == 1 and len(self.merkleRoots) >= self.WorkQueueSizeRegular[1]:
				self.onBlockUpdate()
				self.needMerkle = False
			self._doing('regular merkle roots')
			self.merkleRoots.append(self.makeMerkleRoot(self.currentMerkleTree))
		else:
			if self.needMerkle == 1:
				self.onBlockUpdate()
				self.needMerkle = False
			self._doing('idle')
			# TODO: rather than sleepspin, block until MinimumTxnUpdateWait expires or threading.Condition(?)
			sleep(self.IdleSleepTime)
		if self.needMerkle == 1 and now > self.needMerkleSince + self.WarningDelayTxnLongpoll:
			self._floodWarning(now, 'NeedMerkle', lambda: 'Transaction-longpoll requested %d seconds ago, and still not ready. Is your server fast enough to keep up with your configured WorkQueueSizeRegular maximum?' % (now - self.needMerkleSince,))
		if now > self.nextMerkleUpdate + self.WarningDelayMerkleUpdate:
			self._floodWarning(now, 'MerkleUpdate', lambda: "Haven't updated the merkle tree in at least %d seconds! Is your server fast enough to keep up with your configured work queue minimums?" % (now - self.lastMerkleUpdate,))
	
	def run(self):
		while True:
			try:
				self.merkleMaker_I()
			except:
				self.logger.critical(traceback.format_exc())
	
	def start(self, *a, **k):
		self._prepare()
		super().start(*a, **k)
	
	def getMRD(self):
		(prevBlock, bits) = self.currentBlock
		try:
			MRD = self.merkleRoots.pop()
			self.LowestMerkleRoots = min(len(self.merkleRoots), self.LowestMerkleRoots)
			rollPrevBlk = False
		except IndexError:
			qsz = self.clearMerkleRoots.qsize()
			if qsz < 0x10:
				self.logger.warning('clearMerkleRoots running out! only %d left' % (qsz,))
			MRD = self.clearMerkleRoots.get()
			self.LowestClearMerkleRoots = min(self.clearMerkleRoots.qsize(), self.LowestClearMerkleRoots)
			rollPrevBlk = True
		(merkleRoot, merkleTree, cb) = MRD
		return (merkleRoot, merkleTree, cb, prevBlock, bits, rollPrevBlk)
	
	def getMC(self):
		(prevBlock, bits) = self.currentBlock
		mt = self.currentMerkleTree
		cbpfx = mt.coinbasePrefix
		cb = self.makeCoinbase(cbpfx)
		return (None, mt, cb, prevBlock, bits)
	
# merkleMaker tests
def _test():
	global now
	now = 1337039788
	MM = merkleMaker()
	reallogger = MM.logger
	class fakelogger:
		LO = False
		def critical(self, *a):
			if self.LO > 1: return
			reallogger.critical(*a)
		def warning(self, *a):
			if self.LO: return
			reallogger.warning(*a)
	MM.logger = fakelogger()
	class NMTClass:
		pass
	
	# _figureTimeRules tests
	def FTR(MP):
		NMT = NMTClass
		MM._figureTimeRules(MP, NMT)
		H = {}
		for k in dir(NMT):
			if k[0] == '_':
				continue
			H[k] = getattr(NMT, k)
		return H
	assert FTR({}) == {
		'jobExpire': 1337039880,
		'timeOffset': 0,
		'mintime': now,
		'mintimeOffset': -300,
		'maxtime': inf,
		'maxtimeOffset': 7200,
	}
	assert FTR({
		'expires': 65,
	}) == {
		'jobExpire': 1337039843,
		'timeOffset': 0,
		'mintime': now,
		'mintimeOffset': -300,
		'maxtime': inf,
		'maxtimeOffset': 7200,
	}
	assert FTR({
		'expires': 65,
		'curtime': 1337039789,
	}) == {
		'jobExpire': 1337039843,
		'timeOffset': 1,
		'mintime': now + 1,
		'mintimeOffset': -300,
		'maxtime': inf,
		'maxtimeOffset': 7200,
	}
	assert FTR({
		'mintime': 1337039780,
	}) == {
		'jobExpire': 1337039880,
		'timeOffset': 0,
		'mintime': 1337039780,
		'mintimeOffset': -300,
		'maxtime': inf,
		'maxtimeOffset': 7200,
	}
	assert FTR({
		'mintimeoff': -100,
	}) == {
		'jobExpire': 1337039880,
		'timeOffset': 0,
		'mintime': now,
		'mintimeOffset': -100,
		'maxtime': inf,
		'maxtimeOffset': 7200,
	}
	assert FTR({
		'mintime': now - 200,
		'mintimeoff': -100,
	}) == {
		'jobExpire': 1337039880,
		'timeOffset': 0,
		'mintime': 0,
		'mintimeOffset': -100,
		'maxtime': inf,
		'maxtimeOffset': 7200,
	}
	assert FTR({
		'maxtime': 1337039880,
	}) == {
		'jobExpire': 1337039880,
		'timeOffset': 0,
		'mintime': now,
		'mintimeOffset': -300,
		'maxtime': 1337039880,
		'maxtimeOffset': 7200,
	}
	assert FTR({
		'maxtimeoff': 100,
	}) == {
		'jobExpire': 1337039880,
		'timeOffset': 0,
		'mintime': now,
		'mintimeOffset': -300,
		'maxtime': inf,
		'maxtimeOffset': 100,
	}
	assert FTR({
		'maxtime': 1337039985,
		'maxtimeoff': 100,
	}) == {
		'jobExpire': 1337039880,
		'timeOffset': 0,
		'mintime': now,
		'mintimeOffset': -300,
		'maxtime': inf,
		'maxtimeOffset': 100,
	}
	
	# _makeBlockSafe tests
	from copy import deepcopy
	MP = {
		'coinbasevalue':50,
		'mutable':[],
	}
	txnlist = [b'\0', b'\x01', b'\x02']
	txninfo = [{'fee':0, 'sigops':1}, {'fee':5, 'sigops':10000}, {'fee':0, 'sigops':10001}]
	def MBS(LO = 0):
		m = deepcopy( (MP, txnlist, txninfo) )
		MM.logger.LO = LO
		try:
			MM._makeBlockSafe(*m)
		except:
			if LO < 2:
				raise
		else:
			assert LO < 2  # An expected error wasn't thrown
		return m
	MM.POT = 0
	MBS(2)  # Can't remove transactions
	MP['mutable'].append('transactions')
	assert MBS() == (MP, txnlist[:2], txninfo[:2])
	txninfo[2]['fee'] = 1
	MPx = deepcopy(MP)
	MPx['coinbasevalue'] -= 1
	assert MBS() == (MPx, txnlist[:2], txninfo[:2])
	txninfo[2]['sigops'] = 1
	assert MBS(1) == (MP, txnlist, txninfo)
	txninfo[2]['sigops'] = 10001
	MP['coinbasetxn'] = b''
	MBS(2)  # Can't change generation
	MP['mutable'].append('generation')
	MPx = deepcopy(MP)
	MPx['coinbasevalue'] -= 1
	assert MBS(1) == (MPx, txnlist[:2], txninfo[:2])

_test()
