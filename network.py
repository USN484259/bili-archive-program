#!/usr/bin/env python3

import os
import re
import io
import sys
import time
import httpx
import socket
import shutil
import asyncio
import logging
import hashlib
from contextlib import suppress
from urllib.parse import urlencode

import core
import runtime
import constants

# constants

BILI_DOMAIN = ".bilibili.com"
CHECK_CREDENTIAL_URL = "https://api.bilibili.com/x/web-interface/nav"
WBI_KEY_URL = "https://api.bilibili.com/x/web-interface/nav"

WBI_MIXIN_KEY_ENC_TAB = [
46, 47, 18,  2, 53,  8, 23, 32,
15, 50, 10, 31, 58,  3, 45, 35,
27, 43,  5, 49, 33,  9, 42, 19,
29, 28, 14, 39, 12, 38, 41, 13,
37, 48,  7, 16, 24, 55, 40, 61,
26, 17,  0,  1, 60, 51, 30,  4,
22, 25, 54, 21, 56, 59,  6, 63,
57, 62, 11, 36, 20, 34, 44, 52
]

# static objects

logger = logging.getLogger("bili_arch.network")
wbi_pattern = re.compile(r"^.+/([^/.]+)\.[^/.]+$")
wbi_cached_key = None

## wbi sign
# https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/misc/sign/wbi.md

def md5sum(*params):
	hash_args = {}
	if sys.hexversion >= 0x03090000:
		m = hashlib.md5(usedforsecurity = False)
	else:
		m = hashlib.md5()

	for s in params:
		m.update(s.encode())
	return m.hexdigest()


async def get_wbi_key(sess):
	response = await sess.request("GET", WBI_KEY_URL)
	response.raise_for_status()
	result = response.json()

	wbi_info = result.get("data").get("wbi_img")
	img_key = wbi_pattern.fullmatch(wbi_info.get("img_url")).group(1)
	sub_key = wbi_pattern.fullmatch(wbi_info.get("sub_url")).group(1)

	logger.debug("img_key %s, sub_key %s", img_key, sub_key)
	key_str = img_key + sub_key
	wbi_key = "".join((key_str[k] for k in WBI_MIXIN_KEY_ENC_TAB))
	logger.debug("wbi_key %s", wbi_key)
	return wbi_key[:32]


def wbi_sign_request(wbi_key, kwargs):
	params = kwargs.get("params")
	if not params:
		raise RuntimeError("wbi_sign missing params")

	params = params.copy()
	params["wts"] = int(time.time())
	params = dict(sorted(params.items()))
	query = urlencode(params)
	logger.debug(query)
	params["w_rid"] = md5sum(query, wbi_key)
	result_args = {k: kwargs[k] for k in kwargs if k != "params"}
	result_args["params"] = dict(sorted(params.items()))
	return result_args


## requests

def session(credential = None):
	if credential is None:
		credential = runtime.credential
	timeout = httpx.Timeout(5, connect = runtime.http_timeout)
	cookies = httpx.Cookies()
	for k, v in credential.items():
		cookies.set(k, v, domain = BILI_DOMAIN)

	return httpx.AsyncClient(headers = core.USER_AGENT, timeout = timeout, cookies = cookies, follow_redirects = True)


async def check_credential(sess):
	response = await sess.request("GET", CHECK_CREDENTIAL_URL)
	logger.debug(response)
	result = response.json()
	logger.debug(result)
	code = result.get("code")
	if code != 0:
		raise RuntimeError("bad credential %d" % code)


async def request(sess, method, url, /, wbi_sign = False, **kwargs):
	global wbi_cached_key
	retry = True
	while True:
		if wbi_sign:
			if not wbi_cached_key:
				wbi_cached_key = await get_wbi_key(sess)
			signed_args = wbi_sign_request(wbi_cached_key, kwargs)
		else:
			signed_args = kwargs

		response = await sess.request(method, url, **signed_args)
		response.raise_for_status()
		result = response.json()

		code = result.get("code", -32768)
		if wbi_sign and retry and "v_voucher" in result.get("data", {}):
			wbi_cached_key = await get_wbi_key(sess)
			retry = False
			continue
		elif code == 0:
			return result.get("data")

		msg = result.get("msg") or result.get("message", "")
		logger.error("response code %d, msg %s", code, msg)
		raise RuntimeError(msg)


async def fetch(sess, url, path, **kwargs):
	logger.debug("fetching %s into %s", url, path)
	async with sess.stream("GET", url) as resp:
		logger.debug(resp)
		resp.raise_for_status()

		file_length = None
		length = resp.headers.get('content-length')
		if length:
			length = int(length)
			logger.debug("content length %d", length)
		else:
			logger.warning("missing content-length")

		with core.staged_file(path, "wb", **kwargs) as f:
			last_timestamp = None
			if runtime.bandwidth_limit:
				logger.debug("bandwidth limit %d B/s", runtime.bandwidth_limit)
				last_timestamp = time.monotonic()

			async for chunk in resp.aiter_bytes():
				f.write(chunk)

				if runtime.bandwidth_limit:
					cur_timestamp = time.monotonic()
					time_diff = cur_timestamp - last_timestamp
					expect_time = len(chunk) / runtime.bandwidth_limit
					time_wait = int(expect_time - time_diff)
					if time_diff > 0 and time_wait > 0:
						await asyncio.sleep(time_wait)
						cur_timestamp = time.monotonic()
					last_timestamp = cur_timestamp

			file_length = f.tell()
			logger.debug("EOF with file length %d", file_length)

			if length and file_length != length:
				logger.warning("%s size mismatch, expect %d got %d", path, length, file_length)
				if length > file_length:
					raise RuntimeError("unexpected EOF: %s", path)

	return file_length


async def fetch_stream(sess, url, sink_func = None, *args):
	sink = None
	try:
		async with sess.stream("GET", url) as resp:
			logger.debug(resp)
			resp.raise_for_status()

			async for chunk in resp.aiter_bytes():
				if sink is None:
					logger.debug("calling sink_func")
					sink = sink_func and sink_func(*args) or io.BytesIO()

				sink.write(chunk)

		if not sink_func:
			sink.seek(0)
			return sink

	finally:
		if sink_func and (sink is not None):
			logger.debug("closing sink")
			sink.close()


def create_unix_socket(path, *, mode = 0o600):
	logger.debug("creating unix socket %s", path)
	# https://stackoverflow.com/questions/11781134/change-linux-socket-file-permissions
	sock = socket.socket(socket.AF_UNIX)
	try:
		os.fchmod(sock.fileno(), 0o600)
		with suppress(FileNotFoundError):
			os.unlink(path)
		sock.bind(path)
		os.chmod(path, mode)
		return sock
	except:
		sock.close()
		raise


class image_fetcher:
	def __init__(self, stall_time = 2):
		self.sess = None
		self.task = None
		self.quit = False
		self.queue = asyncio.Queue()
		self.stall = runtime.Stall(stall_time)
		self.cache_map = {}

	async def __aenter__(self):
		await self.start()
		return self

	async def __aexit__(self, exc_type, exc_value, traceback):
		if (exc_type is None and exc_value is None):
			await self.join()
		await self.close()

	async def start(self):
		assert(self.sess is None)
		self.sess = session(credential = {})
		self.task = asyncio.create_task(self.worker())
		self.task.add_done_callback(asyncio.Task.result)

	async def close(self):
		if self.task is not None:
			self.quit = True
			await self.queue.put((None, None, None))
			await self.task
			self.task = None

		if self.sess:
			await self.sess.aclose()
			self.sess = None

	async def join(self):
		if not self.quit:
			await self.queue.join()

	async def schedule(self, path, name, url):
		await self.queue.put((path, name, url))

	async def worker(self):
		while not self.quit:
			if self.queue.empty():
				logger.info("image_fetcher standby")

			path, name, url = await self.queue.get()
			if (path and name and url):
				file_name = os.path.join(path, name)
				if os.path.isfile(file_name):
					logger.debug("skip existing image %s", file_name)
				else:
					cached_file = self.cache_map.get(name)
					try:
						if cached_file:
							logger.debug("reusing image %s", cached_file)
							shutil.copyfile(cached_file, file_name)
						else:
							logger.info("fetching image %s", name)
							await self.stall()
							await fetch(self.sess, url, file_name)
							self.cache_map[name] = file_name
					except Exception as e:
						if cached_file:
							logger.error("failed to copy file %s to %s: %s", cached_file, path, str(e))
						else:
							logger.error("failed to fetch image %s: %s", name, str(e))

			self.queue.task_done()

		logger.info("image_fetcher exited")
