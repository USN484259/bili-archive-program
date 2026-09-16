#!/usr/bin/env python3

# This file is written with the assistance of opencode-deepseek-v4-flash

# live transcription tool.
#
# Two modes:
#   * server mode (-r/--url): GET the pending media list from the FastCGI
#     backend (backend/transcription.py), stream each file over HTTP,
#     transcribe it with whisper-cli (via ffmpeg), PUT the SRT next to the
#     media (as "<media>.srt", e.g. out12.flv -> out12.flv.srt), and POST
#     the API to remove the file from the pending list.
#   * local mode: one or more local media paths; written the same way.
#
# Pipeline per media file:
#
#   source (HTTP range-reconnect reader or local file)
#       |  HttpReader also hashes the stream; the PUT carries the hash, so
#       |  the media is fetched only once (resuming via Range on drops)
#       v
#   feed_worker thread -> ffmpeg stdin
#   ffmpeg  (-i - -vn -ac 1 -ar 16000 -f s16le pipe:1)  -> raw PCM on stdout
#       v
#   WavChunkProducer.get_wav_chunk()   pulls one chunk at a time, wraps it
#       |  in a fresh 44-byte WAV header, trims it later via forward()
#       v
#   Transcriber: whisper-cli (-f - -np -of /dev/null), one process per chunk
#       |  "[hh:mm:ss.mmm --> hh:mm:ss.mmm] text" on stdout, parsed and
#       |  offset by the chunk's start time in the media timeline
#       v
#   convert_to_srt() -> SRT file
#
# Backpressure: transcription pulls chunks synchronously; when whisper is
# slower than ffmpeg the pull stops, ffmpeg's stdout pipe fills up, ffmpeg
# blocks writing, stops reading its stdin, and the feed_worker thread blocks
# on its stdin write, pausing the source reader. Memory stays ~ one chunk.
#
# Chunking: whisper-cli reads stdin until EOF, so each audio chunk gets its
# own process. ffmpeg outputs raw PCM without a header; Python re-adds a
# 44-byte WAV header per chunk so whisper sees a valid WAV file no matter
# where the chunk boundary falls.
#
# Hallucination: whisper may emit 4+ identical, tightly spaced repeated
# segments. We keep the results before the repetition, drop the repeated
# block, and skip the PCM buffer forward past the hallucination point,
# re-running whisper on the remainder (instead of re-transcribing the whole
# chunk), with the time offset advanced accordingly.
#
# NOTE: currently Linux-only. Uses portable Python APIs in the main path,
# but the HTTP reader + the server-side flow target the Linux setup.

import os
import sys
sys.path[0] = os.getcwd()

import re
import time
import struct
import shutil
import hashlib
import logging
import argparse
import subprocess
import threading
import tempfile
import select
from contextlib import suppress
from urllib.parse import urljoin

httpx = None
with suppress(ModuleNotFoundError):
	import httpx

import constants
from utils import logger_init
from messaging import MessagingClient

# constants

bytes_in_MiB = 0x100000
hash_kwargs = (sys.hexversion >= 0x03090000) and {"usedforsecurity": False} or {}
pcm_bytes_per_ms = 32            # 16000 Hz * 2 bytes / 1000 ms
transcribe_targets = (".flv", ".zip", ".mp4", ".m4a")
fallback_dir = "/var/tmp"

hashing_speed_default = 200
put_retry_delay = 30
put_retry_min = 2
put_retry_max = 10

# static objects

logger = logging.getLogger("bili_arch.live_transcribe")
url_pattern = re.compile(r"^https?://")

whisper_bin = shutil.which("whisper-cli")
ffmpeg_bin = shutil.which("ffmpeg")


# helper functions

def make_wav_header(data_size, sample_rate = 16000, channels = 1, bits = 16):
	"""Build a 44-byte PCM WAV header for the given data chunk size."""
	byte_rate = sample_rate * channels * bits // 8
	block_align = channels * bits // 8
	hdr = struct.pack('<4sI4s', b'RIFF', 36 + data_size, b'WAVE')
	hdr += struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, channels, sample_rate, byte_rate, block_align, bits)
	hdr += struct.pack('<4sI', b'data', data_size)
	return hdr


def convert_to_srt(inputs):
	"""Convert [(start_ms, stop_ms, text), ...] to SRT text."""
	format_time = lambda s: "%02d:%02d:%02d,%03d" % (s // (1000*3600), s // (1000*60) % 60, s // 1000 % 60, s % 1000)
	output = []
	index = 1
	for start, stop, text in inputs:
		output += (str(index), "%s --> %s" % (format_time(start), format_time(stop)), text, "")
		index += 1
	output.append("")
	return "\n".join(output)


# output writers

def save_srt(path, srt_content):
	logger.info("saving to %s", path)
	with open(path, "xt", encoding = "utf-8") as f:
		f.write(srt_content)


def save_fallback(rel_name, srt_content):
	"""Server refused the SRT; keep the result somewhere and warn loudly."""
	basename = os.path.basename(rel_name)
	with suppress(OSError):
		os.makedirs(fallback_dir, exist_ok = True)

	fd = None
	try:
		(fd, path) = tempfile.mkstemp(prefix = basename, suffix = ".srt", dir = fallback_dir, text = True)
		os.write(fd, srt_content.encode("utf-8"))
		logger.warning("PUT failed, saved SRT to %s", path)
		return path
	except OSError as e:
		logger.error("cannot save under %s: %s", fallback_dir, e)
	finally:
		if fd is not None:
			os.close(fd)


# ffmpeg pre-process

def spawn_ffmpeg(bin = None):
	cmd = [
		bin or ffmpeg_bin or "ffmpeg",
		"-hide_banner", "-nostats",
		"-i", "-",
		"-vn", "-ac", "1", "-ar", "16000",
		"-f", "s16le",
		"-flush_packets", "1",
		"pipe:1",
	]
	proc = subprocess.Popen(cmd, stdin = subprocess.PIPE, stdout = subprocess.PIPE, bufsize = 0)
	logger.debug("spawned ffmpeg as %d", proc.pid)
	return proc


# whisper-cli wrapper

class WhisperCli:
	def __init__(self, model, /, vad_model = None, no_gpu = False, language = "zh"):
		# whisper_bin is a module global resolved by main() (--whisper or PATH)
		self.cmd = [
			whisper_bin,
			"-m", model,
			"-l", language,
			"-np",                      # print only results
			"-of", "/dev/null",         # no output files, rely on stdout
			"--max-context", "0",       # as in the old transcribe.sh
			"-f", "-",                  # read audio from stdin
		]
		if vad_model:
			self.cmd += ["--vad", "-vm", vad_model]
		if no_gpu:
			self.cmd += ["-ng", "-t", str(os.cpu_count() or 4)]

	def __call__(self):
		# spawn one process per chunk (whisper reads stdin until EOF)
		logger.debug(self.cmd)
		proc = subprocess.Popen(self.cmd, stdin = subprocess.PIPE, stdout = subprocess.PIPE, bufsize = 0)
		logger.debug("spawned whisper-cli as %d", proc.pid)
		return proc


class WavChunkProducer:
	"""Pull-based PCM chunk source.

	Only the ffmpeg stdin feeder runs in a thread; the transcriber pulls
	WAV chunks synchronously via get_wav_chunk(). Each chunk is wrapped in
	a fresh WAV header, and the consumed leading PCM is trimmed with
	forward() once it has been transcribed. Because get_wav_chunk() blocks
	until a full chunk is available, a slow transcriber backpressures
	ffmpeg (stdout pipe fills -> ffmpeg blocks -> feeder stops reading the
	source)."""
	read_size = 0x10000
	def __init__(self, source, chunk_time_ms):
		self.chunk_time_ms = chunk_time_ms
		self.buffer = bytearray()
		self.eof = False
		self.quit = False
		self.error = None
		self.proc = spawn_ffmpeg()
		self.th = threading.Thread(target = self.feed_worker, args = (source, ))
		self.th.start()

	def close(self):
		# stop feeding, tear down ffmpeg, and wait for the feeder thread
		self.quit = True
		self.eof = True
		self.proc.terminate()
		self.proc.wait()
		self.th.join()

	def feed_worker(self, source):
		try:
			pipe = self.proc.stdin
			while not self.quit:
				data = source.read(self.read_size)
				if not data:
					break
				if self.proc.poll() is not None:
					raise RuntimeError("unexpected process %d exit %d", self.proc.pid, self.proc.returncode)
				pipe.write(data)
		except Exception as e:
			self.error = e
		finally:
			pipe.close()

	def get_wav_chunk(self, ms = None):
		# read until a full chunk (default chunk_time_ms) is buffered, then
		# hand it out as a standalone WAV file: 44-byte header + PCM bytes
		size = pcm_bytes_per_ms * (ms or self.chunk_time_ms)
		while not self.eof and len(self.buffer) < size:
			if self.error:
				raise self.error
			data = self.proc.stdout.read(self.read_size)
			if not data:
				self.eof = True
				break
			self.buffer += data
		result_data = self.buffer[:size]
		if not result_data:
			return None
		result_size = len(result_data)
		result = bytearray(make_wav_header(result_size))
		result += result_data
		return result

	def forward(self, ms):
		# discard the leading PCM that has already been transcribed
		offset = ms * pcm_bytes_per_ms
		if offset > len(self.buffer):
			logger.warning("forward size %d exceeds wav buffer %d", offset, len(self.buffer))
		self.buffer[:] = self.buffer[offset:]


class Transcriber:
	"""Runs whisper-cli on one WAV chunk fed via stdin, parses the
	"[hh:mm:ss.mmm --> hh:mm:ss.mmm] text" lines it prints on stdout, and
	watches for hallucination.

	whisper's per-chunk timestamps are 0-based relative to the WAV it was
	given; base_offset_ms is added so results are positioned on the media
	timeline (chunk start, or the forward point after a hallucination)."""

	write_size = 0x10000
	read_size = 0x1000

	output_pattern = re.compile(r"^\[(\d+):(\d+):(\d+)\.(\d+) --> (\d+):(\d+):(\d+)\.(\d+)\]\s+(\S.*)$")
	hallucination_samples = 4
	hallucination_time_threshold = 100

	class HallucinationDetected(RuntimeError):
		pass

	parse_time = staticmethod(
		lambda m, i, base: ((int(m[i+0]) * 60 + int(m[i+1])) * 60 + int(m[i+2])) * 1000 + int(m[i+3]) + base
	)

	def __init__(self, spawner, base_offset_ms):
		self.base_offset_ms = base_offset_ms
		self.proc = spawner()
		self.output = []
		self.line_buffer = ""
		os.set_blocking(self.proc.stdout.fileno(), False)

	def close(self):
		# terminate a whisper that is still running (e.g. aborted mid-chunk
		# on hallucination); an already-exited process is left as-is
		if self.proc.poll() is None:
			self.proc.terminate()
			self.proc.wait()

	def run(self, chunk):
		# `chunk` is consumed in place; interleaving stdout reads with stdin
		# writes keeps whisper's stdout pipe drained while we are still
		# feeding it, avoiding a pipe-full deadlock
		output = []
		line_buffer = ""
		# expects bytearray chunk
		while chunk:
			if self.proc.poll() is not None:
				raise RuntimeError("unexpected process %d exit %d", self.proc.pid, self.proc.returncode)
			self.proc.stdin.write(chunk[:self.write_size])
			chunk[:] = chunk[self.write_size:]
			self.process()
		self.proc.stdin.close()

		poll = select.poll()
		poll.register(self.proc.stdout, select.POLLIN)
		while self.proc.poll() is None:
			poll.poll()
			self.process()

		if self.proc.returncode == 0:
			logger.debug("process %d exit with %d", self.proc.pid, self.proc.returncode)
		else:
			raise RuntimeError("process %d exit with %d", self.proc.pid, self.proc.returncode)

		while self.process() is not None:
			pass

	def get_result(self):
		return self.output

	def process(self):
		data = None
		with suppress(BlockingIOError):
			data = self.proc.stdout.read(self.read_size)
		if not data:
			return None
		self.line_buffer += data.decode(errors = "ignore")
		lines = self.line_buffer.split('\n')
		if len(lines) <= 1:
			return len(lines)
		self.line_buffer = lines.pop()
		for line in lines:
			self.process_line(line)
		return len(lines)


	def process_line(self, line):
		if not line:
			return
		# raw whisper segment lines streamed at INFO double as live progress
		# for interactive use
		logger.info(line)
		line_match = self.output_pattern.fullmatch(line)
		if not line_match:
			logger.warning("cannot parse line:\t%s", line)
			return
		start_time = self.parse_time(line_match, 1, self.base_offset_ms)
		stop_time = self.parse_time(line_match, 5, self.base_offset_ms)
		content = line_match.group(9)
		self.output.append((start_time, stop_time, content))
		self.detect_hallucination()


	def detect_hallucination(self):
		if len(self.output) < self.hallucination_samples:
			return
		samples = self.output[-self.hallucination_samples:]
		for i in range(self.hallucination_samples - 1):
			if samples[i+1][2] != samples[0][2]:
				return

		diff_time = tuple(samples[i+1][0] - samples[i][1] for i in range(self.hallucination_samples - 1))
		if max(diff_time) - min(diff_time) > self.hallucination_time_threshold:
			return

		logger.warning("hallucination at %d", self.output[-1][0])
		# drop everything from the onset of the repeated block; it will be
		# re-transcribed from that point by a fresh whisper-cli process
		del self.output[-self.hallucination_samples:]
		raise self.HallucinationDetected()


# source readers (HTTP with reconnect, or local file)

class HttpReader:
	"""Stream a media file over HTTP for ffmpeg.

	If the connection drops mid-transfer, a new request with a Range header
	resumes exactly where we stopped (no re-download), and the raw bytes are
	hashed on the fly so the PUT can verify the file without a second fetch."""

	range_pattern = re.compile(r"^bytes (\d+)-(\d+)/")
	retry_count = 5

	def __init__(self, sess, url, hash_name = None):
		self.sess = sess
		self.url = url
		self.offset = 0
		self.file_size = None
		self.iterator = None
		self.buffer = bytearray()
		self.eof = False
		self.hash_func = hash_name and hashlib.new(hash_name, **hash_kwargs)

	def close(self):
		self.iterator = None

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def get_size(self):
		return self.file_size

	def get_hash(self):
		return self.hash_func and self.hash_func.hexdigest()

	def create_iterator(self):
		# start (or, via Range, resume) fetching the media; the Range request
		# is what makes a dropped connection recover without re-downloading
		logger.debug("fetching %s offset %d", self.url, self.offset)
		headers = (self.offset > 0) and {"Range": "bytes=%d-" % self.offset} or {}
		with self.sess.stream("GET", self.url, headers = headers) as resp:
			resp.raise_for_status()
			if self.offset > 0:
				range_str = resp.headers.get("Content-Range", "")
				range_match = self.range_pattern.match(range_str)
				if not range_match or int(range_match.group(1)) != self.offset:
					raise RuntimeError("content-range mismatch: %d, %s" % (self.offset, range_str))

			resp_size = None
			with suppress(ValueError, TypeError):
				resp_size = int(resp.headers.get("Content-Length"))
			if resp_size is not None:
				total_size = self.offset + resp_size
				if self.file_size is not None and total_size != self.file_size:
					logger.warning("file size changed %d/%d", total_size, self.file_size)
				self.file_size = total_size

			for data in resp.iter_bytes():
				yield data


	def read(self, size = None):
		if size and size < 0:
			size = None

		if size and len(self.buffer) >= size:
			res = self.buffer[:size]
			self.buffer = self.buffer[size:]
			return res

		if self.eof or (not size and self.buffer):
			res = self.buffer
			self.buffer = bytearray()
			return res

		for retry in range(self.retry_count):
			if self.iterator is None:
				self.iterator = self.create_iterator()

			try:
				data = next(self.iterator)
				if self.hash_func is not None:
					self.hash_func.update(data)
				self.offset += len(data)
				if not size or (not self.buffer and len(data) <= size):
					return data
				else:
					self.buffer += data
					break
			except StopIteration:
				self.eof = True
				if not size:
					return b''
				break
			except httpx.TransportError as e:
				logger.warning("http stream error: %s", str(e))

			logger.warning("retry http stream %d/%d, offset %d", retry, self.retry_count, self.offset)
			self.iterator = None
			time.sleep(1)
		else:
			raise RuntimeError("cannot read http stream after %d retries" % self.retry_count)

		res = self.buffer[:size]
		self.buffer = self.buffer[size:]
		return res


class LocalFileReader:
	def __init__(self, path):
		self.file = open(path, "rb")
		self.size = None
		with suppress(OSError):
			self.size = os.path.getsize(path)

	def close(self):
		self.file.close()

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def get_size(self):
		return self.size

	def get_hash(self):
		return None

	def read(self, size = None):
		if size is None or size < 0:
			return self.file.read()
		return self.file.read(size)


# public interface
# chunk time in seconds
def transcribe(source, whisper, chunk_time):
	"""Transcribe `source` in ~chunk_time-second chunks and return the
	combined [(start_ms, stop_ms, text), ...] segments.

	Audio time is tracked by accumulated PCM length: every chunk advances
	time_offset_ms by chunk_time_ms (or, on hallucination, only past the
	point where the repeated block started), so whisper's per-chunk
	0-based timestamps line up with the media timeline."""
	chunk_time_ms = chunk_time * 1000
	producer = WavChunkProducer(source, chunk_time_ms)
	try:
		time_offset_ms = 0
		results = []
		while True:
			chunk = producer.get_wav_chunk()
			if not chunk:
				break
			logger.debug("transcribing chunk at %d ms, %d bytes", time_offset_ms, len(chunk))
			transcriber = Transcriber(whisper, time_offset_ms)
			try:
				transcriber.run(chunk)
				time_offset_ms += chunk_time_ms
				producer.forward(chunk_time_ms)
			except Transcriber.HallucinationDetected:
				# drop the repeated block, then skip forward past the
				# hallucination point instead of re-transcribing it; the next
				# loop iteration feeds whisper with the remaining PCM
				forw_time = 1000
				res = transcriber.get_result()
				if res:
					last_time = res[-1][1]
					forw_time = max(forw_time, last_time - time_offset_ms)
				time_offset_ms += forw_time
				producer.forward(forw_time)
				logger.info("hallucination handled, continue at %d ms", time_offset_ms)
			finally:
				results += transcriber.get_result()
				transcriber.close()
		return results

	finally:
		producer.close()


# server mode

def put_srt(sess, srt_url, params, srt_content, retry):
	"""PUT the SRT, retrying on 503.

	503 means the backend is still hashing a large media file; the caller
	sizes `retry` from the server-reported hashing speed, and each attempt
	pauses put_retry_delay seconds to give the hash worker time to finish."""

	for attempt in range(retry):
		resp = sess.put(srt_url, params = params, headers = {"content-type": "application/x-subrip"},
		                data = srt_content.encode("utf-8"))
		logger.debug("PUT %s: %d %s", srt_url, resp.status_code, resp.text)
		if resp.status_code != 503:
			return resp.status_code
		logger.warning("server busy (503) on %s, retry %d/%d", srt_url, attempt + 1, retry)
		time.sleep(put_retry_delay)
	return resp.status_code


def http_main(args, msg_client):
	with httpx.Client(timeout = httpx.Timeout(120, connect = 10)) as sess:
		api_url = args.url
		resp = sess.get(api_url)
		resp.raise_for_status()
		info = resp.json()
		logger.debug(info)

		hash_methods = info.get("hash") or []
		if args.hash:
			if args.hash not in hash_methods:
				logger.warning("%s not accepted by server: %s", args.hash, ", ".join(hash_methods))
			hash_name = args.hash
		else:
			hash_name = hash_methods and hash_methods[0] or None

		media_list = info.get("list", [])
		# the server may report hashing_speed as None (e.g. no hashes configured
		# or the worker has not measured yet); fall back to a sane default used
		# only for sizing the PUT 503 retry budget below
		hashing_speed = info.get("hashing_speed") or hashing_speed_default
		whisper = WhisperCli(args.model, args.vad, args.no_gpu)
		logger.info("server mode: %d media file(s), hash %s", len(media_list), hash_name or "none (size check)")

		for media_url in media_list:
			try:
				(name, ext) = os.path.splitext(media_url)
				if not name or ext not in transcribe_targets:
					logger.warning("unsupported media type %s", media_url)
					continue
				srt_url = media_url + ".srt"

				logger.info("transcribing %s", media_url)
				with HttpReader(sess, urljoin(api_url, media_url), hash_name) as source:
					segments = transcribe(source, whisper, args.chunk_time)
					file_size = source.get_size()
					hash_value = source.get_hash()

				srt_content = convert_to_srt(segments)
				logger.info("%s done with %d lines", media_url, len(segments))
				logger.debug("size %d, %s %s", file_size, hash_name, hash_value)

				params = {}
				retries = put_retry_min

				if file_size is not None:
					params["size"] = file_size
					# approximate how many put_retry_delay-second pauses the backend
					# needs to hash the whole file; sizes the 503 retry budget
					retries = 1 + file_size // (put_retry_delay * hashing_speed * bytes_in_MiB)
					retries = min(max(retries, put_retry_min), put_retry_max)

				if hash_name:
					params[hash_name] = hash_value

				path = None
				resp_code = put_srt(sess, urljoin(api_url, srt_url), params, srt_content, retries)
				if resp_code == 201:
					logger.info("PUT ok %d for %s, removing from list", resp_code, media_url)
					try:
						sess.post(api_url, json = {"del": [media_url]})
					except httpx.HTTPError as e:
						logger.warning("cannot remove %s from list: %s", media_url, str(e))
					path = srt_url
				else:
					logger.error("PUT %s failed with %d", srt_url, resp_code)
					path = save_fallback(srt_url, srt_content)

				if path and msg_client:
					msg_client.send(constants.topic.transcribe, json = {
						"server":	api_url,
						"media":	media_url,
						"status":	resp_code,
						"srt_path":		path,
					})
			except Exception:
				logger.exception("exception in transcribing %s", media_url)


# local mode

def local_main(args, msg_client):
	whisper = WhisperCli(args.model, args.vad, args.no_gpu)
	for path in args.files:
		if not os.path.isfile(path):
			logger.warning("not a file: %s", path)
			continue
		logger.info("transcribing %s", path)
		try:
			with LocalFileReader(path) as source:
				segments = transcribe(source, whisper, args.chunk_time)
			srt_content = convert_to_srt(segments)
			srt_path = path + ".srt"      # e.g. a.flv -> a.flv.srt
			try:
				save_srt(srt_path, srt_content)
				logger.info("%s done with %d lines", path, len(segments))
			except OSError as e:
				logger.error("cannot save %s: %s", srt_path, e)
				srt_path = save_fallback(srt_path, srt_content)

			if msg_client:
				msg_client.send(constants.topic.transcribe, json = {
					"media":	path,
					"srt_path":	srt_path,
				})

		except Exception:
			logger.exception("exception in transcribing %s", path)


# main entry

def main(args):
	global whisper_bin
	global ffmpeg_bin
	global fallback_dir

	if args.hash and args.hash not in hashlib.algorithms_available:
		raise ValueError("invalid hash method %s" % args.hash)
	whisper_bin = args.whisper or whisper_bin
	if not whisper_bin:
		raise FileNotFoundError("cannot find whisper-cli")
	ffmpeg_bin = args.ffmpeg or ffmpeg_bin
	if not ffmpeg_bin:
		raise FileNotFoundError("cannot find ffmpeg")
	if not os.path.isfile(args.model):
		raise FileNotFoundError(args.model)
	fallback_dir = args.fallback or fallback_dir

	msg_client = MessagingClient(args.msg_addr)

	if args.url and url_pattern.match(args.url):
		if args.files:
			logger.warning("running server mode, ignoring local files")
		if httpx is None:
			raise RuntimeError("missing httpx library")
		http_main(args, msg_client)
	elif args.files:
		local_main(args, msg_client)
	else:
		raise ValueError("specify --url (server mode) or local files")


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description = "live transcription with whisper.cpp")

	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("--model", required = True, help = "whisper model path (whisper-large-v3)")
	parser.add_argument("--vad", help = "VAD model path (ggml-silero)")
	parser.add_argument("--whisper", help = "whisper-cli binary path")
	parser.add_argument("--ffmpeg", help = "ffmpeg binary path")
	parser.add_argument("--no-gpu", action = "store_true", help = "disable GPU and use all CPU threads")
	parser.add_argument("-t", "--chunk-time", type = int, default = 600, help = "audio chunk length in seconds (default 600)")
	parser.add_argument("-r", "--url", help = "server API URL (server mode)")
	parser.add_argument("--hash", help = "force hash method for server PUT verification")
	parser.add_argument("--fallback", help = "fallback folder to save SRT files when PUT fails")
	parser.add_argument("--msg_addr", help = "messaging server address to broadcast transcription status")
	parser.add_argument("files", nargs = '*', help = "local media files")

	args = parser.parse_args()
	logger_init(args.verbose)

	main(args)
