#!/usr/bin/env python3

import sys
import os
import re
import time
import fcntl
import asyncio
import httpx
import traceback
import argparse
import json
import logging
import collections

# constants

UNIT_TABLE = {
	'k': 1000,
	'ki': 0x400,
	'm': 1000 * 1000,
	'mi': 0x100000,
	'g': 1000 * 1000 * 1000,
	'gi': 0x40000000,
}

LOG_FORMAT = "%(asctime)s\t%(process)d\t%(levelname)s\t%(name)s\t%(message)s"

USER_AGENT = {
	"User-Agent": "Mozilla/5.0",
	"Referer": "https://www.bilibili.com/"
}

BILI_DOMAIN = ".bilibili.com"
CHECK_CREDENTIAL_URL = "https://api.bilibili.com/x/web-interface/nav"

default_name_map = {
	"danmaku": "danmaku.xml",
	"tmp_ext": ".tmp",
	"noaudio": ".noaudio",
	"hls_index": "index.m3u8",
}

# static objects

http_timeout = 20
default_stall_time = 5
bandwidth_limit = None
root_dir = "."

logger = logging.getLogger("bili_arch.core")

default_names = collections.namedtuple("DefaultName", default_name_map.keys())(**default_name_map)
bv_pattern = re.compile(r"(BV\w+)")

# helper functions

def load_credential(auth_file):
	parser = re.compile(r"(\S+)[\s\=\:]+(\S+)\s*")
	cookies = httpx.Cookies()
	logger.debug("auth file at " + auth_file)
	with open(auth_file, "r") as f:
		for line in f:
			match = parser.fullmatch(line)
			if match:
				cookies.set(match.group(1), match.group(2), domain = BILI_DOMAIN)

	return cookies


# public methods

## startup & runtime

def parse_args(arg_list = []):
	global http_timeout
	global default_stall_time
	global bandwidth_limit
	global root_dir

	parser = argparse.ArgumentParser()
	parser.add_argument("-r", "--root")
	parser.add_argument("-u", "--credential")
	parser.add_argument("-t", "--timeout", type = int)
	parser.add_argument("-s", "--stall", type = float)
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("-q", "--quiet", action = "count", default = 0)
	parser.add_argument("-w", "--bandwidth")
	parser.add_argument("-l", "--log")

	for args, kwargs in arg_list:
		parser.add_argument(*args, **kwargs)

	args = parser.parse_args()

	log_level = logging.INFO + 10 * (args.quiet - args.verbose)
	logging.basicConfig(level = log_level, format = LOG_FORMAT)
	root_logger = logging.getLogger()

	if args.log:
		handler = logging.FileHandler(args.log, delay = True)
		handler.setFormatter(logging.Formatter(LOG_FORMAT))
		root_logger.addHandler(handler)

	if args.timeout:
		http_timeout = int(args.timeout)

	if args.stall:
		default_stall_time = float(args.stall)

	if args.bandwidth:
		match = re.fullmatch(r"(\d+)([kKmMgG][Ii]?)?[Bb]?", args.bandwidth)
		bandwidth_limit = int(match.group(1)) * UNIT_TABLE.get(match.group(2).lower(), 1)

	if args.root:
		root_dir = args.root

	logger.debug(args)

	# keep credential safe, load after print
	if args.credential:
		args.credential = load_credential(args.credential)
	else:
		args.credential = {}

	return args


class Stall:
	def __init__(self, stall_time = default_stall_time):
		self.stall_time = stall_time
		self.last_time = 0
		self.mutex = asyncio.Lock()

	async def __call__(self):
		async with self.mutex:
			cur_time = time.monotonic()
			time_diff = cur_time - self.last_time
			time_wait = self.stall_time - time_diff
			logger.debug("stall %f sec",  max(time_wait, 0))
			if time_diff > 0 and time_wait > 0:
				await asyncio.sleep(time_wait)
				cur_time = time.monotonic()
			self.last_time = cur_time


def list_bv(path):
	bv_list = []
	for f in os.listdir(path):
		if bv_pattern.fullmatch(f):
			bv_list.append(f)

	return bv_list


def report(key, status, *args):
	print(key[0].upper(), status, *args, flush = True)


## file management

def touch(path):
	logger.debug("touch " + path)
	open(path, "ab").close()


def mkdir(path):
	logger.debug("mkdir " + path)
	os.makedirs(path, exist_ok = True)


def subdir(key):
	path = os.path.join(root_dir, key)
	mkdir(path)
	return path


def locked_file(filename, mode, **kwargs):
	f = open(filename, mode = mode, **kwargs)
	if 'r' in mode and '+' not in mode:
		lock_mode = fcntl.LOCK_SH
	else:
		lock_mode = fcntl.LOCK_EX

	try:
		fcntl.flock(f, lock_mode | fcntl.LOCK_NB)
		return f
	except:
		f.close()
		raise


class staged_file:
	def __init__(self, filename, mode = 'r', **kwargs):
		self.filename = filename
		self.tmp_name = None
		open_mode = mode
		if 'w' in mode:
			self.tmp_name = filename + default_names.tmp_ext
			logger.debug("using stage file " + self.tmp_name)
			touch(self.tmp_name)
			open_mode = "r+" + (('b' in mode) and 'b' or '')

		self.f = locked_file(self.tmp_name or self.filename, mode = open_mode, **kwargs)
		self.closed = False
		if 'w' in mode:
			try:
				self.f.truncate(0)
				self.f.seek(0)
			except:
				self.f.close()
				raise

	def __enter__(self):
		return self.f

	def __exit__(self, exc_type, exc_value, traceback):
		self.close(exc_type is None and exc_value is None)

	def close(self, replace = True):
		if self.closed:
			return
		try:
			if replace and self.tmp_name:
				logger.debug("move " + self.tmp_name + " to " + self.filename)
				os.replace(self.tmp_name, self.filename)
		finally:
			self.f.close()
			self.closed = True


class locked_path(os.PathLike):
	def __init__(self, *path_list, shared = False):
		self.path = os.path.join(*path_list)
		mkdir(self.path)
		self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
		lock_mode = (shared and fcntl.LOCK_SH or fcntl.LOCK_EX)
		try:
			fcntl.flock(self.fd, lock_mode | fcntl.LOCK_NB)
		except:
			os.close(self.fd)
			raise

	def __fspath__(self):
		return self.path

	def __str__(self):
		return self.path

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		self.close()

	def close(self):
		if self.fd is not None:
			os.close(self.fd)
			self.fd = None


## requests

def session(cookies):
	timeout = httpx.Timeout(5, connect = http_timeout)
	return httpx.AsyncClient(headers = USER_AGENT, timeout = timeout, cookies = cookies, follow_redirects = True)


async def check_credential(sess):
	response = await sess.request("GET", CHECK_CREDENTIAL_URL)
	logger.debug(response)
	result = response.json()
	logger.debug(result)
	code = result.get("code")
	if code != 0:
		raise Exception("bad credential %d" % code)


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
	raise Exception(msg)


async def fetch(sess, url, path):
	logger.debug("fetching " + url + " into " + path)
	async with sess.stream("GET", url) as resp:
		logger.debug(resp)
		resp.raise_for_status()

		file_length = None
		length = resp.headers.get('content-length')
		if length:
			logger.debug("content length " + length)
			length = int(length)
		else:
			logger.warning("missing content-length")


		with staged_file(path, "wb") as f:
			last_timestamp = None
			if bandwidth_limit:
				logger.debug("bandwidth limit " + str(bandwidth_limit) + " byte/sec")
				last_timestamp = time.monotonic()

			async for chunk in resp.aiter_bytes():
				f.write(chunk)

				if bandwidth_limit:
					cur_timestamp = time.monotonic()
					time_diff = cur_timestamp - last_timestamp
					expect_time = len(chunk) / bandwidth_limit
					time_wait = int(expect_time - time_diff)
					if time_diff > 0 and time_wait > 0:
						await asyncio.sleep(time_wait)
						cur_timestamp = time.monotonic()
					last_timestamp = cur_timestamp

			file_length = f.tell()
			logger.debug("EOF with file length " + str(file_length))

			if length and file_length != length:
				logger.warning("%s size mismatch, expect %d got %d", path, length, file_length)
				if length > file_length:
					raise Exception("unexpected EOF " + path)

	return file_length

