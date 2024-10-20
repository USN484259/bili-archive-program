#!/usr/bin/env python3

import os
import time
import json
import zlib
import brotli
import struct
import asyncio
import logging
from collections import namedtuple
from base64 import b64encode
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

import core
import runtime
import network

# constants

LIVE_DANMAKU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
LIVE_HEARTBEAT_URL = "https://live-trace.bilibili.com/xlive/rdata-interface/v1/heartbeat/webHeartBeat"

# static objects

logger = logging.getLogger("bili_arch.live_danmaku")

# helper functions

async def get_live_danmaku_info(sess, rid):
	info = await network.request(sess, "GET", LIVE_DANMAKU_INFO_URL, params = {"id": rid})
	return info

async def get_live_heartbeat(sess, rid, interval):
	hb_str = "%d|%s|1|0" % (interval, rid)
	hb_b64 = b64encode(hb_str.encode())
	info = await network.request(sess, "GET", LIVE_HEARTBEAT_URL, params = {"hb": hb_b64.decode(), "pf": "web"})
	return info

# methods

class LiveDanmaku:
	Header = namedtuple("LiveDanmakuHeader", ("size", "header_size", "protocol", "opcode", "sequence"))
	def __init__(self, rid, *, stall_interval = 2):
		self.rid = str(rid)
		self.sess = None
		self.stall = runtime.Stall(stall_interval)
		self.conn = None
		self.hb_task = None

	async def __aenter__(self):
		self.sess = network.session()
		await self.start()
		return self

	async def __aexit__(self, exc_type, exc_value, traceback):
		await self.close()
		await self.sess.aclose()
		self.sess = None

	async def start(self):
		assert(self.sess)
		info = await get_live_danmaku_info(self.sess, self.rid)
		logger.debug(info)
		self.token = info.get("token")
		self.hosts = info.get("host_list")
		logger.info("room %s found %d danmaku hosts", self.rid, len(self.hosts))
		self.host_index = await self.connect(0)

	async def close(self):
		if self.conn is not None:
			try:
				await self.conn.close()
				await self.conn.wait_closed()
			finally:
				self.conn = None
		if self.hb_task is not None:
			try:
				self.hb_task.cancel()
			except:
				pass
			self.hb_task = None

	async def connect(self, start_index):
		index = start_index
		while True:
			try:
				await self.close()
				host_info = self.hosts[index]
				url = "wss://%s:%d/sub" % (host_info.get("host"), host_info.get("wss_port"))
				await self.stall()
				logger.info("connecting to live danmaku %s [%d/%d]", self.rid, index + 1, len(self.hosts))
				self.conn = await ws_connect(url, user_agent_header = network.USER_AGENT["User-Agent"])
				await self.send_verity()
				return index
			except Exception:
				logger.exception("failed to connect live danmaku")
				await self.close()
				index = (index + 1) % len(self.hosts)
				if index == start_index:
					raise

	async def read(self):
		# FIXME support TCP streams ?
		# websocket has message boundaries, so recv always returns one full packet
		# if use TCP streams, due to the package format, message boundaries are hard to detect
		data = await self.conn.recv()
		header = self.Header(*struct.unpack_from(">IHHII", data))
		return header, data[header.header_size : header.size]

	async def write(self, data, /, protocol = 1, opcode = 2):
		header = struct.pack(">IHHII", 16 + len(data), 16, protocol, opcode, 1)
		return await self.conn.send(header + data)

	async def send_verity(self):
		if not runtime.credential:
			raise NotImplementedError("LiveDanmaku requires credential")

		info = {
			"uid": int(runtime.credential.get("DedeUserID")),
			"roomid": int(self.rid),
			"protover": 3,
			"buvid": runtime.credential.get("buvid3"),
			"platform": "web",
			"type": 2,
			"key": self.token,
		}

		logger.info("sending verity %s", self.rid)
		data = json.dumps(info).encode()
		await self.write(data, protocol = 1, opcode = 7)

	async def check_verity(self, data):
		try:
			info = json.loads(data.decode())
			code = info.get("code")
			if code == 0:
				logger.info("connected to live danmaku %s", self.rid)
				if self.hb_task is None:
					self.hb_task = asyncio.create_task(self.heartbeat())
					self.hb_task.add_done_callback(asyncio.Task.result)
				return True
			else:
				logger.error("verity failed %s", str(code))
		except Exception:
			pass
		logger.error("dropping connection")
		await self.conn.close()

	async def heartbeat(self):
		interval = 60
		while True:
			try:
				data = b"[object Object]"
				logger.info("heartbeat %s", self.rid)
				await self.write(data)
				info = await get_live_heartbeat(self.sess, self.rid, interval)
				interval = info.get("next_interval")
			except Exception:
				logger.exception("failed to send heartbeat")
				interval = 60
			await asyncio.sleep(interval)

	# public methods:

	def __aiter__(self):
		return self

	async def __anext__(self):
		while True:
			try:
				header, data = await self.read()
				result = await self.parse(header, data)
				if not result:
					continue
				return result
			except WebSocketException:
				try:
					self.host_index = await self.connect(self.host_index)
				except WebSocketException:
					await self.start()
			except Exception:
				logger.exception("failed to read socket")
				# prevent busy loop
				await self.stall()

	async def parse(self, header, data):
		logger.debug("packet type %d, size %d", header.protocol, len(data))
		result = []
		if header.protocol == 0:
			# plain data
			result.append(json.loads(data.decode()))
		elif header.protocol == 1:
			logger.debug("packet opcode %d", header.opcode)
			if header.opcode == 8:
				# verity reply
				await self.check_verity(data)
			elif header.opcode == 3:
				# heartbeat reply
				result.append({"views": struct.unpack_from(">I", data)[0]})
		elif header.protocol == 2:
			# zlib
			data = zlib.decompress(data)
			result.append(json.loads(data.decode()))
		elif header.protocol == 3:
			# brotli
			data = brotli.decompress(data)
			offset = 0
			while offset < len(data):
				try:
					logger.debug("reading packet at %d/%d", offset, len(data))
					header = self.Header(*struct.unpack_from(">IHHII", data, offset = offset))
					result += await self.parse(header, data[offset + header.header_size : offset + header.size])
					offset += header.size
				except Exception:
					logger.exception("failed to parse brotli packet")
					break
		return result