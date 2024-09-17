#!/usr/bin/env python3

import time
import httpx
import asyncio
import logging

import core
import runtime

# constants

USER_AGENT = {
	"User-Agent": "Mozilla/5.0",
	"Referer": "https://www.bilibili.com/"
}

BILI_DOMAIN = ".bilibili.com"
CHECK_CREDENTIAL_URL = "https://api.bilibili.com/x/web-interface/nav"


# static objects

logger = logging.getLogger("bili_arch.network")


## requests

def session(credential = None):
	credential = credential or runtime.credential
	timeout = httpx.Timeout(5, connect = runtime.http_timeout)
	cookies = httpx.Cookies()
	for k, v in credential.items():
		cookies.set(k, v, domain = BILI_DOMAIN)

	return httpx.AsyncClient(headers = USER_AGENT, timeout = timeout, cookies = cookies, follow_redirects = True)


async def check_credential(sess):
	response = await sess.request("GET", CHECK_CREDENTIAL_URL)
	logger.debug(response)
	result = response.json()
	logger.debug(result)
	code = result.get("code")
	if code != 0:
		raise RuntimeError("bad credential %d" % code)


async def request(sess, method, url, **kwargs):
	# TODO wbi sign
	response = await sess.request(method, url, **kwargs)
	response.raise_for_status()
	result = response.json()

	code = result.get("code", -32768)
	if code == 0:
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

