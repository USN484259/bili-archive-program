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
import bilibili_api
from bilibili_api import Credential

logger = logging.getLogger("bili_arch.util")

log_format = "%(asctime)s\t%(process)d\t%(levelname)s\t%(name)s\t%(message)s"

agent = {
	"User-Agent": "Mozilla/5.0",
	"Referer": "https://www.bilibili.com/"
}

sec_to_ns = 1000 * 1000 * 1000

unit_table = {
	'k': 1000,
	'ki': 0x400,
	'm': 1000 * 1000,
	'mi': 0x100000,
	'g': 1000 * 1000 * 1000,
	'gi': 0x40000000,
}

tmp_postfix = ".tmp"
noaudio_stub = ".noaudio"

http_timeout = 30

stall_mutex = asyncio.Lock()
stall_duration = 1
stall_timestamp = 0

bandwidth_limit = None
root_dir = "."


async def stall(second = None):
	global stall_mutex
	global stall_timestamp

	async with stall_mutex:
		timestamp = time.monotonic_ns()
		time_diff = timestamp - stall_timestamp
		time_wait = int((second or stall_duration) * sec_to_ns) - time_diff
		logger.debug("stall %dns",  max(time_wait, 0))
		if time_diff > 0 and time_wait > 0:
			await asyncio.sleep(time_wait / sec_to_ns)
			timestamp = time.monotonic_ns()
		stall_timestamp = timestamp


def mkdir(path):
	logger.debug("mkdir " + path)
	os.makedirs(path, exist_ok = True)


def subdir(key):
	path = os.path.join(root_dir, key)
	mkdir(path)
	return path

def report(key, status, *args):
	print(key[0].upper(), status, *args, flush = True)


def touch(path):
	logger.debug("touch " + path)
	open(path, "ab").close()


def list_bv(path):
	bv_pattern = re.compile(r"BV\w+")
	bv_list = []
	for f in os.listdir(path):
		if bv_pattern.fullmatch(f):
			bv_list.append(f)

	return bv_list


def parse_args(arg_list = []):
	global http_timeout
	global stall_duration
	global stall_timestamp
	global bandwidth_limit
	global root_dir

	parser = argparse.ArgumentParser()
	# parser.add_argument("-d", "--dest")
	parser.add_argument("-r", "--root")
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
	logging.basicConfig(level = log_level, format = log_format)
	root_logger = logging.getLogger()

	if args.log:
		handler = logging.FileHandler(args.log, delay = True)
		handler.setFormatter(logging.Formatter(log_format))
		root_logger.addHandler(handler)

	if args.timeout:
		http_timeout = int(args.timeout)

	if args.stall:
		stall_duration = float(args.stall)
	stall_timestamp = time.monotonic_ns()

	if args.bandwidth:
		match = re.fullmatch(r"(\d+)([kKmMgG][Ii]?)?", args.bandwidth)
		bandwidth_limit = int(match.group(1)) * unit_table.get(match.group(2).lower(), 1)

	if args.root:
		root_dir = args.root

	logger.debug(args)
	return args


def run(func):
	# return asyncio.get_event_loop().run_until_complete(func)
	# return bilibili_api.sync(func)
	try:
		return asyncio.run(func)
	except:
		logger.exception("excption in asyncio::run")


async def wait_online():
	sess = bilibili_api.get_session()
	count = 0
	while True:
		try:
			count += 1
			logger.info("waiting online %d", count)
			await stall()
			resp = await sess.head("https://www.bilibili.com")
			resp.raise_for_status()

			logger.info("already online")
			return

		except httpx.HTTPError:
			pass
		except Exception:
			logger.exception("exception in waiting online")
			raise


async def credential(auth_file):
	parser = re.compile(r"(\S+)[\s\=\:]+(\S+)\s*")
	info = dict()
	logger.debug("auth file at " + auth_file)
	with open(auth_file, "r") as f:
		for line in f:
			match = parser.fullmatch(line)
			if match:
				info[match.group(1)] = match.group(2)

	credential = Credential(**info)
	if not await credential.check_valid():
		raise Exception("bad Credential")

	return credential


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

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		self.close()

	def close(self):
		if self.fd is not None:
			os.close(self.fd)
			self.fd = None


class staged_file:
	def __init__(self, filename, mode = 'r', **kwargs):
		self.filename = filename
		self.tmp_name = None
		open_mode = mode
		if tmp_postfix and ('w' in mode):
			self.tmp_name = filename + tmp_postfix
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


def save_json(obj, path):
	logger.debug("saving json " + path)
	with staged_file(path, "w") as f:
		json.dump(obj, f, indent = '\t', ensure_ascii = False)


async def fetch(url, path):
	sess = bilibili_api.get_session()
	logger.debug("fetching " + url + " into " + path)
	async with sess.stream("GET", url, headers=agent, timeout = http_timeout) as resp:
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
				last_timestamp = time.monotonic_ns()

			async for chunk in resp.aiter_bytes():
				f.write(chunk)

				if bandwidth_limit:
					cur_timestamp = time.monotonic_ns()
					time_diff = cur_timestamp - last_timestamp
					expect_time = sec_to_ns * len(chunk) / bandwidth_limit
					time_wait = int(expect_time - time_diff)
					if time_diff > 0 and time_wait > 0:
						await asyncio.sleep(time_wait / sec_to_ns)
						cur_timestamp = time.monotonic_ns()
					last_timestamp = cur_timestamp

			file_length = f.tell()
			logger.debug("EOF with file length " + str(file_length))

			if length and file_length != length:
				logger.warning("%s size mismatch, expect %d got %d", path, length, file_length)
				if length > file_length:
					raise Exception("unexpected EOF " + path)

	return file_length

