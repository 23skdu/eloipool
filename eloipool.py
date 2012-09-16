#!/usr/bin/python3
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

import config

if not hasattr(config, 'ServerName'):
	config.ServerName = 'Unnamed Eloipool'

if not hasattr(config, 'ShareTarget'):
	config.ShareTarget = 0x00000000ffffffffffffffffffffffffffffffffffffffffffffffffffffffff


import logging

if len(logging.root.handlers) == 0:
	logging.basicConfig(
		format='%(asctime)s\t%(name)s\t%(levelname)s\t%(message)s',
		level=logging.DEBUG,
	)
	for infoOnly in ('checkShare', 'JSONRPCHandler', 'merkleMaker', 'Waker for JSONRPCServer', 'JSONRPCServer'):
		logging.getLogger(infoOnly).setLevel(logging.INFO)

def RaiseRedFlags(reason):
	logging.getLogger('redflag').critical(reason)
	return reason


from bitcoin.node import BitcoinLink, BitcoinNode
bcnode = BitcoinNode(config.UpstreamNetworkId)
bcnode.userAgent += b'Eloipool:0.1/'

import jsonrpc
UpstreamBitcoindJSONRPC = jsonrpc.ServiceProxy(config.UpstreamURI)


from bitcoin.script import BitcoinScript
from bitcoin.txn import Txn
from base58 import b58decode
from struct import pack
import subprocess
from time import time

def makeCoinbaseTxn(coinbaseValue, useCoinbaser = True):
	txn = Txn.new()
	
	if useCoinbaser and hasattr(config, 'CoinbaserCmd') and config.CoinbaserCmd:
		coinbased = 0
		try:
			cmd = config.CoinbaserCmd
			cmd = cmd.replace('%d', str(coinbaseValue))
			p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
			nout = int(p.stdout.readline())
			for i in range(nout):
				amount = int(p.stdout.readline())
				addr = p.stdout.readline().rstrip(b'\n').decode('utf8')
				pkScript = BitcoinScript.toAddress(addr)
				txn.addOutput(amount, pkScript)
				coinbased += amount
		except:
			coinbased = coinbaseValue + 1
		if coinbased >= coinbaseValue:
			logging.getLogger('makeCoinbaseTxn').error('Coinbaser failed!')
			txn.outputs = []
		else:
			coinbaseValue -= coinbased
	
	pkScript = BitcoinScript.toAddress(config.TrackerAddr)
	txn.addOutput(coinbaseValue, pkScript)
	
	# TODO
	# TODO: red flag on dupe coinbase
	return txn


import jsonrpc_getwork
from util import Bits2Target

workLog = {}
userStatus = {}
networkTarget = None
DupeShareHACK = {}

server = None
def updateBlocks():
	server.wakeLongpoll()

def blockChanged():
	global DupeShareHACK
	DupeShareHACK = {}
	jsonrpc_getwork._CheckForDupesHACK = {}
	global MM, networkTarget, server
	bits = MM.currentBlock[2]
	if bits is None:
		networkTarget = None
	else:
		networkTarget = Bits2Target(bits)
	workLog.clear()
	updateBlocks()


from time import sleep, time
import traceback

def _WorkLogPruner_I(wl):
	now = time()
	pruned = 0
	for username in wl:
		userwork = wl[username]
		for wli in tuple(userwork.keys()):
			if now > userwork[wli][1] + 120:
				del userwork[wli]
				pruned += 1
	WorkLogPruner.logger.debug('Pruned %d jobs' % (pruned,))

def WorkLogPruner(wl):
	while True:
		try:
			sleep(60)
			_WorkLogPruner_I(wl)
		except:
			WorkLogPruner.logger.error(traceback.format_exc())
WorkLogPruner.logger = logging.getLogger('WorkLogPruner')


from merklemaker import merkleMaker
MM = merkleMaker()
MM.__dict__.update(config.__dict__)
MM.clearCoinbaseTxn = makeCoinbaseTxn(5000000000, False)  # FIXME
MM.clearCoinbaseTxn.assemble()
MM.makeCoinbaseTxn = makeCoinbaseTxn
MM.onBlockChange = blockChanged
MM.onBlockUpdate = updateBlocks


from binascii import b2a_hex
from copy import deepcopy
from math import log
from merklemaker import MakeBlockHeader
from struct import pack, unpack
import threading
from time import time
from util import RejectedShare, dblsha, hash2int, swap32, target2pdiff
import jsonrpc
import traceback

gotwork = None
if hasattr(config, 'GotWorkURI'):
	gotwork = jsonrpc.ServiceProxy(config.GotWorkURI)

if not hasattr(config, 'DynamicTargetting'):
	config.DynamicTargetting = 0
else:
	if not hasattr(config, 'DynamicTargetWindow'):
		config.DynamicTargetWindow = 120
	config.DynamicTargetGoal *= config.DynamicTargetWindow / 60

def submitGotwork(info):
	try:
		gotwork.gotwork(info)
	except:
		checkShare.logger.warning('Failed to submit gotwork\n' + traceback.format_exc())

def getTarget(username, now):
	if not config.DynamicTargetting:
		return None
	if username in userStatus:
		status = userStatus[username]
	else:
		userStatus[username] = [None, now, 0]
		return None
	(targetIn, lastUpdate, work) = status
	if work <= config.DynamicTargetGoal:
		if now < lastUpdate + config.DynamicTargetWindow and (targetIn is None or targetIn >= networkTarget):
			return targetIn
		if not work:
			if targetIn:
				getTarget.logger.debug("No shares from '%s', resetting to minimum target")
				userStatus[username] = [None, now, 0]
			return None
	
	deltaSec = now - lastUpdate
	target = targetIn or config.ShareTarget
	target = int(target * config.DynamicTargetGoal * deltaSec / config.DynamicTargetWindow / work)
	if target >= config.ShareTarget:
		target = None
	else:
		if target < networkTarget:
			target = networkTarget
		if config.DynamicTargetting == 2:
			# Round target to a power of two :)
			target = 2**int(log(target, 2) + 1) - 1
		if target == config.ShareTarget:
			target = None
	if target != targetIn:
		pfx = 'Retargetting %s' % (repr(username),)
		tin = targetIn or config.ShareTarget
		getTarget.logger.debug("%s from: %064x (pdiff %s)" % (pfx, tin, target2pdiff(tin)))
		tgt = target or config.ShareTarget
		getTarget.logger.debug("%s   to: %064x (pdiff %s)" % (pfx, tgt, target2pdiff(tgt)))
	userStatus[username] = [target, now, 0]
	return target
getTarget.logger = logging.getLogger('getTarget')

def RegisterWork(username, wli, wld):
	now = time()
	target = getTarget(username, now)
	wld = tuple(wld) + (target,)
	workLog.setdefault(username, {})[wli] = (wld, now)
	return target or config.ShareTarget

def getBlockHeader(username):
	MRD = MM.getMRD()
	merkleRoot = MRD[0]
	hdr = MakeBlockHeader(MRD)
	workLog.setdefault(username, {})[merkleRoot] = (MRD, time())
	target = RegisterWork(username, merkleRoot, MRD)
	return (hdr, workLog[username][merkleRoot], target)

def getBlockTemplate(username):
	MC = MM.getMC()
	(dummy, merkleTree, coinbase, prevBlock, bits) = MC[:5]
	wliPos = coinbase[0] + 2
	wliLen = coinbase[wliPos - 1]
	wli = coinbase[wliPos:wliPos+wliLen]
	target = RegisterWork(username, wli, MC)
	return (MC, workLog[username][wli], target)

loggersShare = []

RBDs = []
RBPs = []

from bitcoin.varlen import varlenEncode, varlenDecode
import bitcoin.txn
from merklemaker import assembleBlock

def blockSubmissionThread(payload, blkhash):
	myblock = (blkhash, payload[4:36])
	payload = b2a_hex(payload).decode('ascii')
	nexterr = 0
	while True:
		try:
			rv = UpstreamBitcoindJSONRPC.submitblock(payload)
			break
		except:
			try:
				rv = UpstreamBitcoindJSONRPC.getmemorypool(payload)
				if rv is True:
					rv = None
				elif rv is False:
					rv = 'rejected'
				break
			except:
				pass
			now = time()
			if now > nexterr:
				# FIXME: This will show "Method not found" on pre-BIP22 servers
				RaiseRedFlags(traceback.format_exc())
				nexterr = now + 5
			if MM.currentBlock[0] not in myblock:
				RaiseRedFlags('Giving up on submitting block upstream')
				return
	if rv:
		# FIXME: The returned value could be a list of multiple responses
		RaiseRedFlags('Upstream block submission failed: %s' % (rv,))

def checkShare(share):
	shareTime = share['time'] = time()
	
	data = share['data']
	data = data[:80]
	(prevBlock, height, bits) = MM.currentBlock
	sharePrevBlock = data[4:36]
	if sharePrevBlock != prevBlock:
		if sharePrevBlock == MM.lastBlock[0]:
			raise RejectedShare('stale-prevblk')
		raise RejectedShare('bad-prevblk')
	
	# TODO: use userid
	username = share['username']
	if username not in workLog:
		raise RejectedShare('unknown-user')
	
	if data[72:76] != bits:
		raise RejectedShare('bad-diffbits')
	
	# Note that we should accept miners reducing version to 1 if they don't understand 2 yet
	# FIXME: When the supermajority is upgraded to version 2, stop accepting 1!
	if data[1:4] != b'\0\0\0' or data[0] > 2:
		raise RejectedShare('bad-version')
	
	shareMerkleRoot = data[36:68]
	if 'blkdata' in share:
		pl = share['blkdata']
		(txncount, pl) = varlenDecode(pl)
		cbtxn = bitcoin.txn.Txn(pl)
		othertxndata = cbtxn.disassemble(retExtra=True)
		coinbase = cbtxn.getCoinbase()
		wliPos = coinbase[0] + 2
		wliLen = coinbase[wliPos - 1]
		wli = coinbase[wliPos:wliPos+wliLen]
		mode = 'MC'
		moden = 1
	else:
		wli = shareMerkleRoot
		mode = 'MRD'
		moden = 0
	
	MWL = workLog[username]
	if wli not in MWL:
		raise RejectedShare('unknown-work')
	(wld, issueT) = MWL[wli]
	share[mode] = wld
	
	if data in DupeShareHACK:
		raise RejectedShare('duplicate')
	DupeShareHACK[data] = None
	
	blkhash = dblsha(data)
	if blkhash[28:] != b'\0\0\0\0':
		raise RejectedShare('H-not-zero')
	blkhashn = hash2int(blkhash)
	
	global networkTarget
	logfunc = getattr(checkShare.logger, 'info' if blkhashn <= networkTarget else 'debug')
	logfunc('BLKHASH: %64x' % (blkhashn,))
	logfunc(' TARGET: %64x' % (networkTarget,))
	
	workMerkleTree = wld[1]
	workCoinbase = wld[2]
	workTarget = wld[6] if len(wld) > 6 else None
	
	# NOTE: this isn't actually needed for MC mode, but we're abusing it for a trivial share check...
	txlist = workMerkleTree.data
	txlist = [deepcopy(txlist[0]),] + txlist[1:]
	cbtxn = txlist[0]
	cbtxn.setCoinbase(workCoinbase)
	cbtxn.assemble()
	
	if blkhashn <= networkTarget:
		logfunc("Submitting upstream")
		RBDs.append( deepcopy( (data, txlist, share.get('blkdata', None), workMerkleTree) ) )
		if not moden:
			payload = assembleBlock(data, txlist)
		else:
			payload = share['data'] + share['blkdata']
		logfunc('Real block payload: %s' % (b2a_hex(payload).decode('utf8'),))
		RBPs.append(payload)
		threading.Thread(target=blockSubmissionThread, args=(payload, blkhash)).start()
		bcnode.submitBlock(payload)
		share['upstreamResult'] = True
		MM.updateBlock(blkhash)
	
	# Gotwork hack...
	if gotwork and blkhashn <= config.GotWorkTarget:
		try:
			coinbaseMrkl = cbtxn.data
			coinbaseMrkl += blkhash
			steps = workMerkleTree._steps
			coinbaseMrkl += pack('B', len(steps))
			for step in steps:
				coinbaseMrkl += step
			coinbaseMrkl += b"\0\0\0\0"
			info = {}
			info['hash'] = b2a_hex(blkhash).decode('ascii')
			info['header'] = b2a_hex(data).decode('ascii')
			info['coinbaseMrkl'] = b2a_hex(coinbaseMrkl).decode('ascii')
			thr = threading.Thread(target=submitGotwork, args=(info,))
			thr.daemon = True
			thr.start()
		except:
			checkShare.logger.warning('Failed to build gotwork request')
	
	if workTarget is None:
		workTarget = config.ShareTarget
	if blkhashn > workTarget:
		raise RejectedShare('high-hash')
	share['target'] = workTarget
	share['_targethex'] = '%064x' % (workTarget,)
	
	shareTimestamp = unpack('<L', data[68:72])[0]
	if shareTime < issueT - 120:
		raise RejectedShare('stale-work')
	if shareTimestamp < shareTime - 300:
		raise RejectedShare('time-too-old')
	if shareTimestamp > shareTime + 7200:
		raise RejectedShare('time-too-new')
	
	if config.DynamicTargetting and username in userStatus:
		# NOTE: userStatus[username] only doesn't exist across restarts
		status = userStatus[username]
		target = status[0] or config.ShareTarget
		if target == workTarget:
			userStatus[username][2] += 1
		else:
			userStatus[username][2] += float(target) / workTarget
	
	if moden:
		cbpre = cbtxn.getCoinbase()
		cbpreLen = len(cbpre)
		if coinbase[:cbpreLen] != cbpre:
			raise RejectedShare('bad-cb-prefix')
		
		# Filter out known "I support" flags, to prevent exploits
		for ff in (b'/P2SH/', b'NOP2SH', b'p2sh/CHV', b'p2sh/NOCHV'):
			if coinbase.find(ff) > max(-1, cbpreLen - len(ff)):
				raise RejectedShare('bad-cb-flag')
		
		if len(coinbase) > 100:
			raise RejectedShare('bad-cb-length')
		
		cbtxn.setCoinbase(coinbase)
		cbtxn.assemble()
		if shareMerkleRoot != workMerkleTree.withFirst(cbtxn):
			raise RejectedShare('bad-txnmrklroot')
		
		if len(othertxndata):
			allowed = assembleBlock(data, txlist)[80:]
			if allowed != share['blkdata']:
				raise RejectedShare('bad-txns')
checkShare.logger = logging.getLogger('checkShare')

def receiveShare(share):
	# TODO: username => userid
	try:
		checkShare(share)
	except RejectedShare as rej:
		share['rejectReason'] = str(rej)
		raise
	finally:
		if '_origdata' in share:
			share['solution'] = share['_origdata']
		else:
			share['solution'] = b2a_hex(swap32(share['data'])).decode('utf8')
		for i in loggersShare:
			i(share)

def newBlockNotification():
	logging.getLogger('newBlockNotification').info('Received new block notification')
	MM.updateMerkleTree()
	# TODO: Force RESPOND TO LONGPOLLS?
	pass

def newBlockNotificationSIGNAL(signum, frame):
	# Use a new thread, in case the signal handler is called with locks held
	thr = threading.Thread(target=newBlockNotification, name='newBlockNotification via signal %s' % (signum,))
	thr.daemon = True
	thr.start()

from signal import signal, SIGUSR1
signal(SIGUSR1, newBlockNotificationSIGNAL)


import os
import os.path
import pickle
import signal
import sys
from time import sleep
import traceback

SAVE_STATE_FILENAME = 'eloipool.worklog'

def stopServers():
	logger = logging.getLogger('stopServers')
	
	if hasattr(stopServers, 'already'):
		logger.debug('Already tried to stop servers before')
		return
	stopServers.already = True
	
	logger.info('Stopping servers...')
	global bcnode, server
	servers = (bcnode, server)
	for s in servers:
		s.keepgoing = False
	for s in servers:
		try:
			s.wakeup()
		except:
			logger.error('Failed to stop server %s\n%s' % (s, traceback.format_exc()))
	i = 0
	while True:
		sl = []
		for s in servers:
			if s.running:
				sl.append(s.__class__.__name__)
		if not sl:
			break
		i += 1
		if i >= 0x100:
			logger.error('Servers taking too long to stop (%s), giving up' % (', '.join(sl)))
			break
		sleep(0.01)
	
	for s in servers:
		for fd in s._fd.keys():
			os.close(fd)

def saveState(t = None):
	logger = logging.getLogger('saveState')
	
	# Then, save data needed to resume work
	logger.info('Saving work state to \'%s\'...' % (SAVE_STATE_FILENAME,))
	i = 0
	while True:
		try:
			with open(SAVE_STATE_FILENAME, 'wb') as f:
				pickle.dump(t, f)
				pickle.dump(DupeShareHACK, f)
				pickle.dump(workLog, f)
			break
		except:
			i += 1
			if i >= 0x10000:
				logger.error('Failed to save work\n' + traceback.format_exc())
				try:
					os.unlink(SAVE_STATE_FILENAME)
				except:
					logger.error(('Failed to unlink \'%s\'; resume may have trouble\n' % (SAVE_STATE_FILENAME,)) + traceback.format_exc())

def exit():
	t = time()
	stopServers()
	saveState(t)
	logging.getLogger('exit').info('Goodbye...')
	os.kill(os.getpid(), signal.SIGTERM)
	sys.exit(0)

def restart():
	t = time()
	stopServers()
	saveState(t)
	logging.getLogger('restart').info('Restarting...')
	try:
		os.execv(sys.argv[0], sys.argv)
	except:
		logging.getLogger('restart').error('Failed to exec\n' + traceback.format_exc())

def restoreState():
	if not os.path.exists(SAVE_STATE_FILENAME):
		return
	
	global workLog, DupeShareHACK
	
	logger = logging.getLogger('restoreState')
	s = os.stat(SAVE_STATE_FILENAME)
	logger.info('Restoring saved state from \'%s\' (%d bytes)' % (SAVE_STATE_FILENAME, s.st_size))
	try:
		with open(SAVE_STATE_FILENAME, 'rb') as f:
			t = pickle.load(f)
			if type(t) == tuple:
				if len(t) > 2:
					# Future formats, not supported here
					ver = t[3]
					# TODO
				
				# Old format, from 2012-02-02 to 2012-02-03
				workLog = t[0]
				DupeShareHACK = t[1]
				t = None
			else:
				if isinstance(t, dict):
					# Old format, from 2012-02-03 to 2012-02-03
					DupeShareHACK = t
					t = None
				else:
					# Current format, from 2012-02-03 onward
					DupeShareHACK = pickle.load(f)
				
				if t + 120 >= time():
					workLog = pickle.load(f)
				else:
					logger.debug('Skipping restore of expired workLog')
	except:
		logger.error('Failed to restore state\n' + traceback.format_exc())
		return
	logger.info('State restored successfully')
	if t:
		logger.info('Total downtime: %g seconds' % (time() - t,))


from jsonrpcserver import JSONRPCListener, JSONRPCServer
import interactivemode
from networkserver import NetworkListener
import threading
import sharelogging
import imp

if __name__ == "__main__":
	if not hasattr(config, 'ShareLogging'):
		config.ShareLogging = ()
	if hasattr(config, 'DbOptions'):
		logging.getLogger('backwardCompatibility').warn('DbOptions configuration variable is deprecated; upgrade to ShareLogging var before 2013-03-05')
		config.ShareLogging = list(config.ShareLogging)
		config.ShareLogging.append( {
			'type': 'sql',
			'engine': 'postgres',
			'dbopts': config.DbOptions,
			'statement': "insert into shares (rem_host, username, our_result, upstream_result, reason, solution) values ({Q(remoteHost)}, {username}, {YN(not(rejectReason))}, {YN(upstreamResult)}, {rejectReason}, decode({solution}, 'hex'))",
		} )
	for i in config.ShareLogging:
		if not hasattr(i, 'keys'):
			name, parameters = i
			logging.getLogger('backwardCompatibility').warn('Using short-term backward compatibility for ShareLogging[\'%s\']; be sure to update config before 2012-04-04' % (name,))
			if name == 'postgres':
				name = 'sql'
				i = {
					'engine': 'postgres',
					'dbopts': parameters,
				}
			elif name == 'logfile':
				i = {}
				i['thropts'] = parameters
				if 'filename' in parameters:
					i['filename'] = parameters['filename']
					i['thropts'] = dict(i['thropts'])
					del i['thropts']['filename']
			else:
				i = parameters
			i['type'] = name
		
		name = i['type']
		parameters = i
		try:
			fp, pathname, description = imp.find_module(name, sharelogging.__path__)
			m = imp.load_module(name, fp, pathname, description)
			lo = getattr(m, name)(**parameters)
			loggersShare.append(lo.logShare)
		except:
			logging.getLogger('sharelogging').error("Error setting up share logger %s: %s", name,  sys.exc_info())

	LSbc = []
	if not hasattr(config, 'BitcoinNodeAddresses'):
		config.BitcoinNodeAddresses = ()
	for a in config.BitcoinNodeAddresses:
		LSbc.append(NetworkListener(bcnode, a))
	
	if hasattr(config, 'UpstreamBitcoindNode') and config.UpstreamBitcoindNode:
		BitcoinLink(bcnode, dest=config.UpstreamBitcoindNode)
	
	import jsonrpc_getblocktemplate
	import jsonrpc_getwork
	import jsonrpc_setworkaux
	
	server = JSONRPCServer()
	if hasattr(config, 'JSONRPCAddress'):
		logging.getLogger('backwardCompatibility').warn('JSONRPCAddress configuration variable is deprecated; upgrade to JSONRPCAddresses list before 2013-03-05')
		if not hasattr(config, 'JSONRPCAddresses'):
			config.JSONRPCAddresses = []
		config.JSONRPCAddresses.insert(0, config.JSONRPCAddress)
	LS = []
	for a in config.JSONRPCAddresses:
		LS.append(JSONRPCListener(server, a))
	if hasattr(config, 'SecretUser'):
		server.SecretUser = config.SecretUser
	server.aux = MM.CoinbaseAux
	server.getBlockHeader = getBlockHeader
	server.getBlockTemplate = getBlockTemplate
	server.receiveShare = receiveShare
	server.RaiseRedFlags = RaiseRedFlags
	server.ShareTarget = config.ShareTarget
	
	if hasattr(config, 'TrustedForwarders'):
		server.TrustedForwarders = config.TrustedForwarders
	server.ServerName = config.ServerName
	
	MM.start()
	
	restoreState()
	
	prune_thr = threading.Thread(target=WorkLogPruner, args=(workLog,))
	prune_thr.daemon = True
	prune_thr.start()
	
	bcnode_thr = threading.Thread(target=bcnode.serve_forever)
	bcnode_thr.daemon = True
	bcnode_thr.start()
	
	server.serve_forever()
