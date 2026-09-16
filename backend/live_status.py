#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import time
import asyncio
import logging
import argparse

import constants
from contextlib import suppress
from utils import logger_init
from simple_fastcgi import AsyncFcgiServer, AsyncHttpResponseMixin, AsyncFcgiHandler
from messaging import MessagingClient

logger = logging.getLogger("bili_arch.live_status")

class live_status_handler(AsyncHttpResponseMixin, AsyncFcgiHandler):
	async def handle(self):
		try:
			result = await self.server.get()
			return await self.send_response(200, json = result)
		except Exception:
			logger.exception("exception in handle")
			return await self.send_response(500)


class LiveStatusServer(AsyncFcgiServer):
	live_status_query_timeout = 2
	def __init__(self, handler, msg_addr, interval):
		if interval <= 0:
			raise ValueError("invalid interval " + str(interval))
		super().__init__(handler)
		self.msg_client = MessagingClient(msg_addr)
		self.interval = interval
		self.cond = asyncio.Condition()
		self.req_timestamp = 0
		self.resp_timestamp = 0
		self.cached_result = {}

		self.msg_client.subscribe(constants.topic.live_status)
		asyncio.get_running_loop().add_reader(self.msg_client.fileno(), self.handle_message)

	async def get(self):
		cur_time = time.time()
		async with self.cond:
			if cur_time - self.resp_timestamp < self.interval:
				return self.cached_result

			if cur_time - self.req_timestamp >= self.interval:
				self.msg_client.send(constants.topic.live_status, json = {
					"timestamp":	cur_time,
					"action":	"get-live-status"
				})
				self.req_timestamp = cur_time

			try:
				await asyncio.wait_for(self.cond.wait(), timeout = self.live_status_query_timeout)
			except asyncio.TimeoutError:
				logger.warning("timeout waiting for live-status")

			return self.cached_result

	async def set_result(self, resp):
		async with self.cond:
			result = resp.get("live_status")
			if not result:
				return
			self.resp_timestamp = time.time()
			self.cached_result = result
			self.cond.notify_all()

	def handle_message(self):
		try:
			self.msg_client.wait(0)
			while True:
				topic, resp = self.msg_client.recv(constants.topic.live_status)
				if not topic:
					return
				if not isinstance(resp, dict) or resp.get("action", "") != "publish-live-status":
					continue
				asyncio.get_running_loop().create_task(self.set_result(resp))

		except Exception:
			logger.exception("exception in handle_message")


async def main(args):
	async with LiveStatusServer(live_status_handler, args.msg_addr, args.status_interval) as server:
		await server.serve_forever()


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("--status-interval", type = int, default = 5)
	parser.add_argument("--msg-addr")
	parser.add_argument("--danmaku-root")
	parser.add_argument("--danmaku-socket")

	args = parser.parse_args()

	# danmaku_server is a standalone service running on the host
	# it works with mod_wstunnel and is not related to fastCGI
	# to make danmaku_server start with lighty, attach it to
	# some FCGI service, such as live_status here
	if args.danmaku_root and args.danmaku_socket:
		pid = os.fork()
		if pid == 0:
			try:
				from danmaku_server import DanmakuServer, danmaku_handler

				with suppress(OSError):
					os.unlink(args.danmaku_socket)
				with DanmakuServer(args.danmaku_root, args.danmaku_socket, danmaku_handler) as server:
					server.serve_forever(poll_interval = 600)

			finally:
				os._exit(0)
		else:
			os.waitpid(pid, os.WNOHANG)

	logger_init(args.verbose)
	asyncio.run(main(args))
