#!/usr/bin/env python3

# std library
import os
import sys
if not getattr(sys, 'frozen', False):
	sys.path[0] = os.getcwd()

import re
import queue
import random
import logging
import asyncio
import tomllib
from threading import Thread
from contextlib import suppress

# project library
import core
import runtime
import network
from live_danmaku import LiveDanmaku

# static objects

CONFIG_VERSION = 1

logger = logging.getLogger("bili_arch.danmaku_reaction")


# parsers

def parse_danmaku(ev):
	info = ev["info"]
	return {
		"type":		"danmaku",
		"uname":	info[2][1],
		"tag":		info[3] and (info[3][1], info[3][0]) or None,
		"text":		info[1],
	}

def parse_superchat(ev):
	data = ev["data"]
	return {
		"type":		"superchat",
		"uname":	data["user_info"]["uname"],
		"tag":		"medal_info" in data and (data["medal_info"]["medal_name"], data["medal_info"]["medal_level"]) or None,
		"price":	data["price"],
		"text":		data["message"],
	}

def parse_captain(ev):
	data = ev["data"]
	if "guard_info" not in data:
		return

	return {
		"type":		"captain",
		"role":		data["guard_info"]["role_name"],
		"uname":	data["sender_uinfo"]["base"]["name"],
		"price":	data["pay_info"]["price"] // 1000,
		"count":	data["pay_info"]["num"],
		"unit_price":	data["pay_info"]["price"] // (1000 * data["pay_info"]["num"]),
		"text":		data["toast_msg"],
	}

parser_table = {
	"DANMU_MSG":		parse_danmaku,
	"SUPER_CHAT_MESSAGE":	parse_superchat,
	"USER_TOAST_MSG_V2":	parse_captain,
}

class Condition:
	def __init__(self, op, key, value):
		self.op = op
		self.key = key
		self.value = value

	def __call__(self, info):
		raise NotImplementedError


class CompareCondition(Condition):
	def __call__(self, info):
		if self.op in ("=", "=="):
			return info[self.key] == self.value
		elif self.op in ("!=", "~=", "<>"):
			return info[self.key] != self.value
		elif self.op == "<":
			return info[self.key] < self.value
		elif self.op == "<=":
			return info[self.key] <= self.value
		elif self.op == ">":
			return info[self.key] > self.value
		elif self.op == ">=":
			return info[self.key] >= self.value
		else:
			raise RuntimeError("unknown op %s", self.op)


class StringFindCondition(Condition):
	def __call__(self, info):
		return (self.value in info[self.key])


class RegexCondition(Condition):
	def __init__(self, *args):
		super().__init__(*args)
		self.pattern = re.compile(self.value)

	def __call__(self, info):
		return bool(self.pattern.match(info[self.key]))


class Action:
	def __init__(self, weight, config):
		self.weight = weight
		self.configure(config)

	def configure(self, config):
		pass

	def __call__(self, info):
		raise NotImplementedError


class NullAction(Action):
	def __call__(self, info):
		pass


class TextOutputAction(Action):
	def configure(self, config):
		self.filename = config.get("file")

	def __call__(self, info):
		text = info["uname"] + '\t' + info.get("text", "")
		if not self.filename:
			print(text)
			return

		with open(self.filename, "a") as f:
			f.write(text)


class PlayAudioAction(Action):
	audio_thread_handle = None
	play_queue = None

	@classmethod
	def audio_thread(cls):
		# windows platform library
		import winsound

		while True:
			filename = "<unknown>"
			try:
				filename = cls.play_queue.get()
				logger.info("playing sound %s", filename)
				winsound.PlaySound(filename, winsound.SND_FILENAME | winsound.SND_NODEFAULT | winsound.SND_NOSTOP)
			except queue.ShutDown:
				break
			except Exception as e:
				logger.error("failed to play sound %s: %s", filename, str(e))

	@classmethod
	def start_audio_thread(cls):
		cls.play_queue = queue.Queue(maxsize = 4)
		thread = Thread(target = cls.audio_thread, daemon = True)
		thread.start()
		cls.audio_thread_handle = thread

	@classmethod
	def play(cls, filename):
		try:
			cls.play_queue.put(filename, block = False)
		except queue.Full:
			logger.warning("play queue full, dropped %s", filename)

	def configure(self, config):
		self.filename = config["file"]

	def __call__(self, info):
		thread_obj = type(self).audio_thread_handle
		if thread_obj is not None and not thread_obj.is_alive():
			logger.warning("audio thread dead, respawning")
			type(self).audio_thread_handle = None

		if type(self).audio_thread_handle is None:
			self.start_audio_thread()
		self.play(self.filename)


action_table = {
	"null":		NullAction,
	"text":		TextOutputAction,
	"play_audio":	PlayAudioAction,
}


class ActionGroup:
	@staticmethod
	def load_action(action):
		func = action["type"]
		weight = action.get("weight", 1)

		action_cls = action_table.get(func)
		if action_cls:
			return action_cls(weight, action)
		else:
			raise ValueError("unknown action " + func)

	def __init__(self, name, config):
		self.name = name
		self.action_list = []
		self.total_weight = 0

		if not isinstance(config, list):
			self.action_list.append(self.load_action(config))
			return

		for action in config:
			action_obj = self.load_action(action)

			self.action_list.append(action_obj)
			self.total_weight += action_obj.weight

	def __call__(self, info):
		rand_number = 0
		if self.total_weight > 0:
			rand_number = random.randrange(self.total_weight)
			logger.debug("rand %d/%d", rand_number, self.total_weight)

		for action in self.action_list:
			rand_number -= action.weight
			if rand_number < 0:
				action(info)
				break


class Rule:
	@staticmethod
	def load_condition(cond):
		key = cond["key"]
		op = cond["op"]
		value = cond["value"]

		if op == "find":
			return StringFindCondition(op, key, value)
		elif op == "regex":
			return RegexCondition(op, key, value)
		elif op in ("=", "==", "!=", "~=", "<>", "<", "<=", ">", ">="):
			return CompareCondition(op, key, value)
		else:
			raise ValueError("unknown op " + op)

	def __init__(self, name, config):
		self.name = name
		self.condition_list = []
		self.action_names = config["action"]

		for cond in config["condition"]:
			self.condition_list.append(self.load_condition(cond))


	def __call__(self, info):
		logger.debug("rule %s", self.name)
		for cond in self.condition_list:
			result = cond(info)
			logger.debug("condition %s", result and "true" or "false")
			if not result:
				return False

		logger.info("matched rule %s", self.name)
		return True


class Dispatcher:
	def __init__(self, config):
		if config["version"] != CONFIG_VERSION:
			raise ValueError("config version %d not supported, expect %d", config["version"], CONFIG_VERSION)
		self.rid = config["room"]
		self.rule_list = []
		self.action_table = {}

		for name, action in config["actions"].items():
			self.action_table[name] = ActionGroup(name, action)

		for name, rule in config["rules"].items():
			if not rule.get("enabled", True):
				continue

			rule = Rule(name, rule)
			for action in rule.action_names:
				if action not in self.action_table:
					raise ValueError("undefined action %s", action)

			self.rule_list.append(rule)


	def __call__(self, ev):
		info = None
		with suppress(KeyError, IndexError):
			cmd = ev["cmd"]
			info = parser_table[cmd](ev)

		if not info:
			return

		logger.debug("got %s", info["type"])

		for rule in self.rule_list:
			try:
				if not rule(info):
					continue
			except Exception as e:
				logger.warning("exception in rule %s: %s", rule.name, str(e))

			for key in rule.action_names:
				try:
					self.action_table[key](info)
				except Exception as e:
					logger.warning("exception in action %s: %s", key, str(e))


async def main(args):
	try:
		with open(args.config, mode = "rb") as f:
			config = tomllib.load(f)

		dispatcher = Dispatcher(config)

		if args.test:
			logger.info("configuration test pass")
			return

		async with LiveDanmaku(dispatcher.rid) as live_danmaku:
			async for ev_list in live_danmaku:
				for ev in ev_list:
					try:
						dispatcher(ev)
					except Exception as e:
						logger.error("failed in dispatcher: %s", str(e))

	except Exception as e:
		logger.error("exception in main: %s", str(e))
		raise

if __name__ == "__main__":
	args = runtime.parse_args(("auth", ), [
		(("--test",), {"action": "store_true"}),
		(("config",), {}),
	])
	asyncio.run(main(args))
