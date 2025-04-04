#!/usr/bin/env python3

import io
import re
import logging

# constants

M3U_HEADER = "#EXTM3U"
M3U_ENDLIST = "#EXT-X-ENDLIST"

M3U_DURATION_PATTERN = r'#EXT-X-TARGETDURATION:(\d+)'
M3U_SEQUENCE_PATTERN = r'#EXT-X-MEDIA-SEQUENCE:\d+'
M3U_MAP_URI_PATTERN = r'#EXT-X-MAP:URI="(.+)"'
M3U_STREAM_INF_PATTERN = r'#EXT-X-STREAM-INF:.*BANDWIDTH=(\d+).*'

# static objects

logger = logging.getLogger("bili_arch.hls")

pattern_duration = re.compile(M3U_DURATION_PATTERN)
pattern_sequence = re.compile(M3U_SEQUENCE_PATTERN)
pattern_map_uri = re.compile(M3U_MAP_URI_PATTERN)
pattern_stream_inf = re.compile(M3U_STREAM_INF_PATTERN)

# helper functions

def check_header(header):
	duration = None
	has_sequence = False
	for line in header:
		dur_match = pattern_duration.fullmatch(line)
		if dur_match:
			duration = int(dur_match.group(1))
		elif pattern_sequence.fullmatch(line):
			has_sequence = True

	return has_sequence and duration


# methods

class M3u:
	def __init__(self):
		self.header = []
		# dict preserve order on python 3.6+
		self.segments = {}
		self.duration = 2


	def parse(self, data):
		result = []
		cur_seg = []
		variant_streams = []
		in_header = True
		stream_inf = None
		eos = False
		while True:
			line = data.readline()
			logger.debug(line)
			if not line:
				break
			line = line.strip()
			if not line:
				continue

			if stream_inf:
				logger.debug("stream_inf uri %s", line)
				variant_streams.append({
					"bandwidth": int(stream_inf.group(1)),
					"uri": line
				})
				stream_inf = None
				continue

			if line == M3U_ENDLIST:
				logger.debug("end of stream")
				eos = True
				continue

			map_uri = pattern_map_uri.fullmatch(line)
			if map_uri:
				uri = map_uri.group(1)
				logger.debug("map_uri %s", uri)
				if uri not in self.segments:
					self.segments[uri] = [line]
					result.append(uri)
					continue

			stream_inf = pattern_stream_inf.fullmatch(line)
			if stream_inf:
				logger.debug("stream_inf bandwidth %d", int(stream_inf.group(1)))
				continue

			cur_seg.append(line)
			if in_header:
				duration = check_header(cur_seg)
				if duration:
					logger.debug("end of header")
					if not self.header:
						self.header = cur_seg
						self.duration = duration
					cur_seg = []
					in_header = False
					continue

			if not line.startswith('#'):
				if line not in self.segments:
					logger.debug("new segment %s", line)
					self.segments[line] = cur_seg
					result.append(line)
				cur_seg = []

		logger.debug("new_segments %d, variant_streams %d", len(result), len(variant_streams))
		if variant_streams and not result:
			result = sorted(variant_streams, key = (lambda obj: obj["bandwidth"]), reverse = True)[0]
			logger.debug("variant_streams %d: %s", result["bandwidth"], result["uri"])
			return result["uri"]

		if eos and not result:
			return None
		return result


	async def async_update(self, request_func, url, *args, **kwargs):
		data = await request_func(url, *args, **kwargs)
		result = self.parse(io.TextIOWrapper(data))
		if result is str:
			logger.debug("fetching variant_stream")
			result = await async_update(self, request_func, result, *args, **kwargs)
		return result


	def dump(self, out):
		if out is not io.TextIOBase:
			out = io.TextIOWrapper(out)

		for line in self.header:
			logger.debug(line)
			out.write(line + '\n')

		for seg in self.segments.values():
			for line in seg:
				logger.debug(line)
				out.write(line + '\n')

		out.write(M3U_ENDLIST + '\n')
