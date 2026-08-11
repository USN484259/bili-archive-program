#!/usr/bin/env python3

import re
import collections

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

default_names = collections.namedtuple("DefaultName", DEFAULT_NAME_MAP.keys())(**DEFAULT_NAME_MAP)

class TopicMeta(type):
	def __getattr__(cls, name):
		return name

class topic(metaclass = TopicMeta):
	pass


__all__ = (
	"LOG_FORMAT",
	"USER_AGENT",
	"bvid_pattern",
	"default_names",
	"topic",
)