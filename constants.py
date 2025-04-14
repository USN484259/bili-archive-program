#!/usr/bin/env python3

import re
import collections

UNIT_TABLE = {
	'k': 1000,
	'ki': 0x400,
	'm': 1000 * 1000,
	'mi': 0x100000,
	'g': 1000 * 1000 * 1000,
	'gi': 0x40000000,
}

DEFAULT_NAME_MAP = {
	"danmaku": "danmaku.xml",
	"tmp_ext": ".tmp",
	"novideo": ".novideo",
	"noaudio": ".noaudio",
	"hls_index": "index.m3u8",
	"rotate_postfix": "-rotate.zip",
	"backup_postfix": ".bak",
	"danmaku_socket": "danmaku.socket",
}

LOG_FORMAT = "%(asctime)s\t%(process)d\t%(levelname)s\t%(name)s\t%(message)s"

USER_AGENT = {
	"User-Agent": "Mozilla/5.0",
	"Referer": "https://www.bilibili.com/"
}


# https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/misc/bvid_desc.md
bvid_pattern = re.compile(r"(BV1[1-9A-HJ-NP-Za-km-z]{9})")
unit_pattern = re.compile(r"(\d+)([kKmMgG][Ii]?)?[Bb]?")

default_names = collections.namedtuple("DefaultName", DEFAULT_NAME_MAP.keys())(**DEFAULT_NAME_MAP)

def number_with_unit(num_str):
	match = unit_pattern.fullmatch(num_str)
	return int(match.group(1)) * UNIT_TABLE.get(match.group(2).lower(), 1)
