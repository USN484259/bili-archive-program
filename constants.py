#!/usr/bin/env python3

import re

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



bv_pattern = re.compile(r"(BV\w+)")
unit_pattern = re.compile(r"(\d+)([kKmMgG][Ii]?)?[Bb]?")


def number_with_unit(num_str):
	match = unit_pattern.fullmatch(num_str)
	return int(match.group(1)) * UNIT_TABLE.get(match.group(2).lower(), 1)
