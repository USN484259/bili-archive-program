#!/usr/bin/env python3

import os
import re
import math
import json
import logging
import shutil
import subprocess
import util
from interactive import is_interactive

try:
	from defusedxml.sax import make_parser as xml_make_parser
except ModuleNotFoundError:
	from xml.sax import make_parser as xml_make_parser

logger = logging.getLogger(__name__)

ffprobe_bin = shutil.which("ffprobe")
ffprobe_options = [
	"-hide_banner",
	"-show_format",
	"-show_streams",
	"-print_format", "json=compact=1"
]

subtitle_pattern = re.compile(r"subtitle\.(.*)\.json")
xml_parser = xml_make_parser()


def ffprobe(path):
	result = {}
	try:
		logger.debug("running ffprobe on " + path)
		cmdline = [ffprobe_bin]
		if logger.isEnabledFor(logging.DEBUG):
			cmdline = cmdline + ["-v", "8"]

		cmdline = cmdline + ffprobe_options + [path]
		logger.log(util.LOG_TRACE, cmdline)
		with subprocess.Popen(cmdline, stdout = subprocess.PIPE) as proc:
			result = json.load(proc.stdout)
			logger.log(util.LOG_TRACE, result)

	except Exception as e:
		logger.exception("exception on ffprobe for " + path)
		util.on_exception(e)

	return result


def verify_media(path, part_duration, duration_tolerance):
	media_info = ffprobe(path)
	try:
		vc = 0
		ac = 0
		for i, media in enumerate(media_info.get("streams")):
			media_type = media.get("codec_type")
			media_duration = media.get("duration", None)
			if not media_duration:
				logger.warning("missing duration in stream-info, use format-info")
				media_duration = media_info.get("format").get("duration")
			media_duration = float(media_duration)

			logger.debug("stream %d, type %s, duration %f", media.get("index"), media_type, media_duration or math.nan)

			if not duration_tolerance:
				logger.debug("skip duration check")
			elif not part_duration:
				logger.warning("unknown duration, skip")
			elif abs(part_duration - media_duration) >= duration_tolerance:
				logger.warning("media duration mismatch, %f/%f", part_duration, media_duration)
				return 0, 0

			if media_type == "video":
				vc += 1
			elif media_type == "audio":
				ac += 1

		return vc, ac
	except Exception as e:
		logger.exception("exception on verify media for " + path)
		util.on_exception(e)
		return 0, 0


def verify_bv(bv, path = None, scan_files = False, duration_tolerance = None):
	if scan_files and not ffprobe_bin:
		raise Exception("ffprobe binary not found")

	result = {
		"info" : False,
		"cover" : False
	}
	try:
		bv_root = util.opt_path(path) + bv + os.path.sep
		logger.debug("verify video %s, path %s, scan %s, tolerance %f", bv, bv_root, scan_files, duration_tolerance or math.nan)

		logger.debug("loading video info")
		with open(bv_root + "info.json") as f:
			bv_info = json.load(f)
			logger.log(util.LOG_TRACE, bv_info)

		logger.info("%s, %s", bv, bv_info.get("title"))

		for ext in [".jpg", ".png", ".gif", ".bmp"]:
			cover_file = bv_root + "cover" + ext
			logger.debug("checking cover " + cover_file)
			if not os.path.isfile(cover_file):
				continue
			if scan_files:
				media_info = ffprobe(cover_file)
				try:
					codec = media_info.get("streams")[0].get("codec_name")
					logger.debug("cover codec " + codec)
					if codec not in ["mjpeg", "png"]:
						continue
				except Exception as e:
					logger.exception("exception on verify cover for " + cover_file)
					util.on_exception(e)
					continue

			logger.debug("found cover, format " + ext)
			result["cover"] = True
			break
		else:
			logger.warning("cover not found")

		part_list = None
		if "interactive" in bv_info:
			if not is_interactive(bv_info):
				raise Exception("conflicting interactive video status")

			part_list = bv_info.get("interactive").get("nodes")

			logger.debug("interactive video, checking graph")
			# TODO scan DOT file
			result["graph"] = os.path.isfile(bv_root + "graph.dot")
		else:
			part_list = bv_info.get("pages")
			if bv_info.get("videos") != len(part_list):
				raise Exception("part count mismatch")

		for part in part_list:
			cid = part.get("cid")
			part_duration = part.get("duration", None)

			logger.info("cid %d, %s, %f sec", cid, part.get("part", None) or part.get("title", ""), part_duration or math.nan)

			video_name = "V:" + str(cid)
			danmaku_name = "D:" + str(cid)
			subtitle_name = "S:" + str(cid)
			result[video_name] = False
			result[danmaku_name] = False

			subtitle = bv_info.get("subtitle")[str(cid)].get("subtitles")
			logger.debug("subtitle count " + str(len(subtitle)))
			logger.log(util.LOG_TRACE, subtitle)
			if len(subtitle) > 0:
				result[subtitle_name] = False

			part_root = bv_root + str(cid) + os.path.sep

			if not os.path.isdir(part_root):
				logger.debug("part dir not exist")
				continue

			video_count = 0
			audio_count = 0
			subtitle_count = 0
			no_audio = False

			logger.debug("checking %d in %s", cid, part_root)
			for filename in os.listdir(part_root):
				logger.debug("file " + filename)
				ext = os.path.splitext(filename)[1].lower()

				if ext == util.tmp_postfix:
					logger.debug("skip tmp file")
					continue

				if filename == util.noaudio_stub:
					logger.warning("found no-audio stub")
					no_audio = True
					continue

				if filename == "danmaku.xml":
					logger.debug("type: danmaku")
					if scan_files:
						try:
							with open(part_root + filename, "r") as f:
								xml_parser.parse(f)
						except:
							logger.warning("unexpected XML format")
							continue

					logger.debug("found danmaku")
					result[danmaku_name] = True
					continue

				if ext == ".json":
					match = subtitle_pattern.fullmatch(filename)
					if not match:
						logger.warning("type: json (unknown)")
						continue

					lan = match.group(1)
					logger.debug("type: subtitle " + lan)
					if scan_files:
						try:
							with open(part_root + filename, "r") as f:
								json.load(f)
						except:
							logger.warning("unexpected JSON format")
							continue

					logger.debug("found subtitle, lang " + lan)
					for i, sub in enumerate(subtitle):
						if sub.get("lan") == lan:
							del subtitle[i]
							subtitle_count += 1
							break
					else:
						logger.warning("subtitle not recorded " + lan)

					continue

				if ext == ".m4v":
					logger.debug("type: video")
					if scan_files:
						vc, ac = verify_media(part_root + filename, part_duration, duration_tolerance)
						if vc != 1 or ac != 0:
							logger.warning("unexpected media streams")
							continue

					logger.debug("found video")
					video_count += 1
					continue

				if ext == ".m4a":
					logger.debug("type: audio")
					if scan_files:
						vc, ac = verify_media(part_root + filename, part_duration, duration_tolerance)
						if vc != 0 or ac != 1:
							logger.warning("unexpected media streams")
							continue

					logger.debug("found audio")
					audio_count += 1
					continue

				if ext == ".flv":
					logger.debug("type: flv")
					if scan_files:
						vc, ac = verify_media(part_root + filename, part_duration, duration_tolerance)
						if vc != 1 or ac != 1:
							logger.warning("unexpected media streams")
							continue

					logger.debug("found video & audio")
					video_count += 1
					audio_count += 1
					continue

				logger.debug("unknown type " + ext)

			logger.debug("video_count %d, audio_count %d, subtitle_count %d", video_count, audio_count, subtitle_count)
			if video_count > 0 and (no_audio or audio_count > 0):
				result[video_name] = True
			else:
				logger.warning("missing media files")

			if not result[danmaku_name]:
				logger.warning("missing danmaku file")

			if subtitle_name in result:
				if len(subtitle) == 0:
					result[subtitle_name] = True
				else:
					logger.warning("missing subtitle %d/%d", len(subtitle), subtitle_count)
					logger.log(util.LOG_TRACE, subtitle)

		logger.debug("verify done for " + bv)
		result["info"] = True
	except Exception as e:
		logger.exception("exception in verifing " + bv)
		util.on_exception(e)

	logger.info(result)
	return result

def main(args):
	if len(args.inputs) > 0:
		logger.debug("%d BV on cmdline", len(args.inputs))
		bv_list = args.inputs
	else:
		logger.debug("scan BV in " + (args.dest or "(cwd)"))
		bv_list = util.list_bv(args.dest)

	logger.info("BV count %d", len(bv_list))
	logger.log(util.LOG_TRACE, bv_list)

	tolerance = args.tolerance
	if tolerance:
		tolerance = float(tolerance)

	for i, bv in enumerate(bv_list):
		result = verify_bv(bv, path = args.dest, scan_files = args.scan, duration_tolerance = tolerance)
		res = True
		for k, v in result.items():
			if not v:
				res = False
				break

		print(bv, res, flush = True)

if __name__ == "__main__":
	args = util.parse_args([
		(("inputs",), {"nargs" : '*'}),
		(("--scan",), {"action" : "store_true"}),
		(("--tolerance",), {"type" : float})
	])
	main(args)
