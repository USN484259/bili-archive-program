#!/usr/bin/env python3

import os
import re
import math
import json
import logging
import shutil
import subprocess
try:
	from defusedxml.sax import make_parser as xml_make_parser
except ModuleNotFoundError:
	from xml.sax import make_parser as xml_make_parser

import core
import runtime


logger = logging.getLogger("bili_arch.verify")

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
		logger.debug("running ffprobe on %s", path)
		cmdline = [ffprobe_bin]
		if not logger.isEnabledFor(logging.DEBUG):
			cmdline = cmdline + ["-v", "8"]

		cmdline = cmdline + ffprobe_options + [path]
		with subprocess.Popen(cmdline, stdout = subprocess.PIPE) as proc:
			result = json.load(proc.stdout)

	except Exception as e:
		logger.exception("exception on ffprobe for %s", path)

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

			logger.debug("stream %d, type %s, duration %.1f/%.1f", media.get("index"), media_type, media_duration or math.nan, part_duration or math.nan)

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
		logger.exception("exception on verify media for %s", path)
		return 0, 0


def verify_cover(path, name, scan_files):
	cover_path = os.path.join(path, name)
	logger.debug("checking cover %s", cover_path)
	if not os.path.isfile(cover_path):
		return False

	if scan_files:
		media_info = ffprobe(cover_path)
		try:
			codec = media_info.get("streams")[0].get("codec_name")
			logger.debug("cover codec %s", codec)
			if codec not in ["mjpeg", "png", "gif", "bmp"]:
				return False

		except Exception as e:
			logger.exception("exception on verify cover for %s", cover_path)
			return False

	logger.debug("found cover %s", name)
	return True


def verify_bv(bv_root, *, ignore = "", autoremove = None, scan_files = False, duration_tolerance = None):
	if scan_files and not ffprobe_bin:
		logger.warning("ffprobe binary not found")
		scan_files = False

	result = {
		"info" : False,
		"parts" : {}
	}
	try:
		logger.debug("verify video %s, scan %s, tolerance %.1f", bv_root, scan_files, duration_tolerance or math.nan)
		if ignore:
			logger.debug("ignore %s", ignore)
		logger.debug("loading video info")
		with open(os.path.join(bv_root, "info.json"), 'r') as f:
			bv_info = json.load(f)

		logger.info("%s, %s", bv_info.get("bvid"), bv_info.get("title"))

		if 'C' not in ignore:
			result["cover"] = False

			cover_name = os.path.split(bv_info.get("pic", ""))[1]
			if cover_name and verify_cover(bv_root, cover_name, scan_files):
				result["cover"] = True
			else:
				logger.warning("cover not found")

		if 'P' in ignore:
			part_list = []
		elif "interactive" in bv_info:
		# 	if not is_interactive(bv_info):
		# 		raise RuntimeError("conflicting interactive video status")

			part_list = bv_info.get("interactive").get("nodes")

			logger.debug("interactive video, checking graph")
			# TODO scan DOT file
			result["graph"] = os.path.isfile(os.path.join(bv_root, "graph.dot"))
		else:
			part_list = bv_info.get("pages")
			if bv_info.get("videos") != len(part_list):
				raise RuntimeError("part count mismatch")

		subtitle_table = None
		if 'S' not in ignore:
			subtitle_table = bv_info.get("subtitle")
			if not subtitle_table:
				logger.warning("missing subtitles for %s", bv_info.get("bvid"))

		for part in part_list:
			cid = str(part.get("cid", ""))
			part_duration = part.get("duration", None)

			logger.info("cid %s, %s, %.1f sec", cid, part.get("part", None) or part.get("title", ""), part_duration or math.nan)

			part_stat = {}
			result["parts"][cid] = part_stat

			if 'V' not in ignore:
				part_stat['V'] = 0
			if 'A' not in ignore:
				part_stat['A'] = 0
			if 'D' not in ignore:
				part_stat['D'] = 0

			if subtitle_table:
				subtitle = subtitle_table.get(cid, {}).get("subtitles", [])
				logger.debug("subtitle count %d", len(subtitle))
				if len(subtitle) > 0:
					part_stat['S'] = 0

			part_root = os.path.join(bv_root, cid)
			if not os.path.isdir(part_root):
				logger.debug("part dir not exist")
				continue

			video_count = 0
			audio_count = 0
			no_audio = False
			no_video = False

			logger.debug("checking %s in %s", cid, part_root)
			for filename in os.listdir(part_root):
				logger.debug("file %s", filename)
				ext = os.path.splitext(filename)[1].lower()

				if ext == core.default_names.tmp_ext:
					logger.debug("skip tmp file")
					continue

				if filename == core.default_names.novideo:
					logger.info("found no-video stub")
					no_video = True
					continue

				if filename == core.default_names.noaudio:
					logger.info("found no-audio stub")
					no_audio = True
					continue

				if filename == core.default_names.danmaku and 'D' not in ignore:
					logger.debug("type: danmaku")
					if scan_files:
						try:
							with open(os.path.join(part_root, filename), "r") as f:
								xml_parser.parse(f)
						except Exception:
							logger.warning("unexpected XML format")
							continue

					logger.debug("found danmaku")
					part_stat['D'] += 1
					continue

				if ext == ".json":
					match = subtitle_pattern.fullmatch(filename)
					if not match:
						logger.warning("type: json (unknown)")
						continue

					if 'S' not in ignore:
						lan = match.group(1)
						logger.debug("type: subtitle %s", lan)
						if scan_files:
							try:
								with open(os.path.join(part_root, filename), "r") as f:
									json.load(f)
							except Exception:
								logger.warning("unexpected JSON format")
								continue

						logger.debug("found subtitle, lang %s", lan)
						for i, sub in enumerate(subtitle):
							if sub.get("lan") == lan:
								del subtitle[i]
								part_stat['S'] += 1
								break
						else:
							logger.warning("subtitle not recorded %s", lan)

					continue

				if 'V' in ignore and 'A' in ignore:
					continue

				if ext == ".m4v" and 'V' not in ignore:
					logger.debug("type: video")
					if scan_files:
						vc, ac = verify_media(os.path.join(part_root, filename), part_duration, duration_tolerance)
						if vc != 1 or ac != 0:
							logger.warning("unexpected media streams")
							continue

					logger.debug("found video")
					video_count += 1
					continue

				if ext == ".m4a" and 'A' not in ignore:
					logger.debug("type: audio")
					if scan_files:
						vc, ac = verify_media(os.path.join(part_root, filename), part_duration, duration_tolerance)
						if vc != 0 or ac != 1:
							logger.warning("unexpected media streams")
							continue

					logger.debug("found audio")
					audio_count += 1
					continue

				if ext == ".flv":
					logger.debug("type: flv")
					if scan_files:
						vc, ac = verify_media(os.path.join(part_root, filename), part_duration, duration_tolerance)
						if vc != 1 or ac != 1:
							logger.warning("unexpected media streams")
							continue

					logger.debug("found video & audio")
					video_count += 1
					audio_count += 1
					continue

				logger.debug("unknown type %s", ext)

			logger.info("video_count %d, audio_count %d", video_count, audio_count)

			if 'V' not in ignore:
				if video_count > 0:
					part_stat['V'] = video_count
				elif no_video:
					del part_stat['V']
				else:
					logger.warning("missing video files")

			if 'A' not in ignore:
				if audio_count > 0:
					part_stat['A'] = audio_count
				elif no_audio:
					del part_stat['A']
				else:
					logger.warning("missing audio files")

			if 'D' not in ignore and not part_stat['D']:
				logger.warning("missing danmaku file")

			if not part_stat.get('S', True):
				logger.warning("missing subtitle")

		logger.debug("verify done for %s", bv_info.get("bvid"))
		result["info"] = True
	except Exception as e:
		logger.exception("exception in verifing %s", bv_root)

	logger.debug(result)
	return result


def check_result(stat):
	def recursive_check(obj):
		if isinstance(obj, dict):
			res = True
			for k, v in obj.items():
				res &= recursive_check(v)
			return res
		else:
			return bool(obj)

	return recursive_check(stat)


def main(args):
	video_root = args.dir or runtime.subdir("video")
	if len(args.inputs) > 0:
		logger.debug("%d BV on cmdline", len(args.inputs))
		bv_list = args.inputs
	else:
		logger.debug("scan BV in %s", video_root)
		bv_list = runtime.list_bv(video_root)

	logger.info("BV count %d", len(bv_list))

	tolerance = args.tolerance
	if tolerance:
		tolerance = float(tolerance)

	verified_count = 0
	for bv in bv_list:
		bv_root = os.path.join(video_root, bv)
		result = verify_bv(bv_root, ignore = args.ignore, scan_files = args.scan, duration_tolerance = tolerance)
		res = check_result(result)
		if res:
			verified_count += 1

		runtime.report("video", res, bv)

	logger.info("finished verify video %d/%d", verified_count, len(bv_list))


if __name__ == "__main__":
	args = runtime.parse_args(("dir",), [
		(("inputs",), {"nargs" : '*'}),
		(("--scan",), {"action" : "store_true"}),
		(("--tolerance",), {"type" : float}),
		(("--ignore",), {"default": ""}),
	])
	main(args)
