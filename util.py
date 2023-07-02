import sys
import os
import re
import time
import asyncio
import httpx
import traceback
import argparse
import json
import logging
import bilibili_api
from bilibili_api import Credential

LOG_TRACE = 2

logging.addLevelName(LOG_TRACE, "TRACE")
logging.basicConfig(level = 0, format = "%(asctime)s\t%(name)s\t%(levelname)s\t%(message)s")
logger = logging.getLogger("util")

agent = {
	"User-Agent": "Mozilla/5.0",
	"Referer": "https://www.bilibili.com/"
}

sec_to_ns = 1000 * 1000 * 1000

unit_table = {
	'b': 1,
	'B': 1,
	'k': 1000,
	'K': 0x400,
	'm': 1000 * 1000,
	'M': 0x100000,
	'g': 1000 * 1000 * 1000,
	'G': 0x40000000
}

http_timeout = 30

stall_mutex = asyncio.Lock()
stall_duration = 1
stall_timestamp = 0

bandwidth_limit = None

tmp_postfix = ".tmp"
noaudio_stub = ".noaudio"

async def stall():
	global stall_mutex
	global stall_timestamp

	async with stall_mutex:
		timestamp = time.monotonic_ns()
		time_diff = timestamp - stall_timestamp
		time_wait = int(stall_duration * sec_to_ns) - time_diff
		logger.debug("stall %dns",  max(time_wait, 0))
		if time_diff > 0 and time_wait > 0:
			await asyncio.sleep(time_wait / sec_to_ns)
			timestamp = time.monotonic_ns()
		stall_timestamp = timestamp


def mkdir(path):
	try:
		logger.debug("mkdir " + path)
		os.mkdir(path)
	except FileExistsError:
		logger.debug("exist" + path)
		pass


def touch(path):
	logger.debug("touch " + path)
	open(path, "a").close()


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

	parser = argparse.ArgumentParser()
	parser.add_argument("-d", "--dest")
	parser.add_argument("-t", "--timeout", type = int)
	parser.add_argument("-s", "--stall", type = float)
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("-q", "--quiet", action = "count", default = 0)
	parser.add_argument("-w", "--bandwidth")

	for args, kwargs in arg_list:
		parser.add_argument(*args, **kwargs)

	args = parser.parse_args()

	log_level = logging.INFO + 10 * (args.quiet - args.verbose)
	logging.getLogger().setLevel(log_level)

	if args.timeout:
		http_timeout = int(args.timeout)

	if args.stall:
		stall_duration = float(args.stall)
	stall_timestamp = time.monotonic_ns()

	if args.bandwidth:
		match = re.fullmatch(r"(\d+)([bBkKmMgG]?)", args.bandwidth)
		bandwidth_limit = int(match.group(1)) * unit_table.get(match.group(2), 'b')

	logger.debug(args)
	logger.log(2, args)
	return args


def opt_path(path):
	if not path:
		return ""
	elif path[-1:] != os.path.sep:
		return path + os.path.sep
	else:
		return path


def on_exception(e):
	if isinstance(e, bilibili_api.exceptions.NetworkException) and e.status == 412:
		logger.critical("encounter flow control, rethrow")
		raise


def run(func):
	# return asyncio.get_event_loop().run_until_complete(func)
	# return bilibili_api.sync(func)
	try:
		return asyncio.run(func)
	except:
		logger.exception("excption in asyncio::run")



async def credential(auth_file):
	parser = re.compile(r"(\S+)[\s\=\:]+(\S+)\s*")
	info = dict()
	logger.debug("auth file at " + auth_file)
	with open(auth_file, "r") as f:
		for line in f:
			match = parser.fullmatch(line)
			if match:
				logger.log(0, "got key " + match.group(1))
				info[match.group(1)] = match.group(2)

	credential = Credential(**info)
	if not await credential.check_valid():
		raise Exception("bad Credential")

	return credential


class staged_file:
	def __init__(self, filename, mode = 'r', **kwargs):
		self.filename = filename
		self.tmp_name = None
		if tmp_postfix and ('w' in mode):
			self.tmp_name = filename + tmp_postfix
			logger.debug("using stage file " + self.tmp_name)

		self.f = open(self.tmp_name or self.filename, mode = mode, **kwargs)

	def __enter__(self):
		return self.f

	def __exit__(self, exc_type, exc_value, traceback):
		self.f.close()
		if exc_type is None and exc_value is None and self.tmp_name:
			logger.debug("move " + self.tmp_name + " to " + self.filename)
			os.replace(self.tmp_name, self.filename)


def save_json(obj, path):
	logger.debug("saving json " + path)
	with staged_file(path, "w") as f:
		json.dump(obj, f, indent = '\t', ensure_ascii = False)


async def fetch(url, path):
	sess = bilibili_api.get_session()
	logger.log(0, "fetching " + url + " into " + path)
	async with sess.stream("GET", url, headers=agent, timeout = http_timeout) as resp:
		logger.log(0, resp)
		resp.raise_for_status()

		file_name = path
		file_length = None
		length = resp.headers.get('content-length')
		if length:
			logger.log(0, "content length " + length)
			length = int(length)
		else:
			logger.warning("missing content-length")

		if tmp_postfix:
			file_name = path + tmp_postfix
			logger.debug("using stage file " + file_name)

		with open(file_name, "wb") as f:
			last_timestamp = None
			if bandwidth_limit:
				logger.debug("bandwidth limit " + str(bandwidth_limit) + " byte/sec")
				last_timestamp = time.monotonic_ns()

			async for chunk in resp.aiter_bytes():
				logger.log(0, '*', raw = True)
				f.write(chunk)

				if bandwidth_limit:
					cur_timestamp = time.monotonic_ns()
					time_diff = cur_timestamp - last_timestamp
					expect_time = sec_to_ns * len(chunk) / bandwidth_limit
					time_wait = int(expect_time - time_diff)
					if time_diff > 0 and time_wait > 0:
						logger.log(0, '<' + str(time_wait) + '>')
						await asyncio.sleep(time_wait / sec_to_ns)
						cur_timestamp = time.monotonic_ns()
					last_timestamp = cur_timestamp

			file_length = f.tell()
			logger.debug("EOF with file length " + str(file_length))

	if length and file_length != length:
		logger.warning("%s size mismatch, expect %d got %d",file_name, length, file_length)
		if length > file_length:
			raise Exception("unexpected EOF " + path)

	if tmp_postfix:
		logger.debug("move " + file_name + " to " + path)
		os.replace(file_name, path)

	return file_length

