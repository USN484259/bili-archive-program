#!/usr/bin/env python3

import os
from collections import deque
from bilibili_api import video, interactive_video
import util


def is_interactive(info):
	score = 0

	if info.get("videos") > 1 and len(info.get("pages")) == 1:
		score += 2

	if info.get("stat").get("evaluation") != "":
		score += 1

	if info.get("no_cache"):
		score += 1

	if not info.get("rights").get("autoplay"):
		score += 1

	if info.get("rights").get("is_stein_gate"):
		score += 1

	if info.get("stein_guide_cid", None) == 99543100:
		score += 1

	util.logv("is_interactive score " + str(score))
	return score > 4


def make_node(cid, edge):
	node = {
		"cid":		cid,
		"title":	edge.get("title"),
		"is_leaf":	(edge.get("is_leaf", 0) > 0)
	}
	if len(edge.get("edges").get("questions", [])) == 0:
		util.logw("non-leaf node missing questions")
		node["is_leaf"] = True

	if not node.get("is_leaf"):
		question = edge.get("edges").get("questions")[0]
		jump_type = question.get("type")
		node["jump_type"] = jump_type
		if jump_type == 2:
			node["countdown"] = question.get("duration")

	return node


def make_edge(src_cid, child):
	edge = {
		"eid":		child.get("id"),
		"src_cid":	src_cid,
		"dst_cid":	child.get("cid"),
		"text":		child.get("option"),
		"is_default":	(child.get("is_default", 0) > 0)
	}

	if "x" in child:
		edge["x"] = child.get("x")

	if "y" in child:
		edge["y"] = child.get("y")

	if "text_align" in child:
		edge["align"] = child.get("text_align")

	condition = child.get("condition")
	if condition != "":
		edge["condition"] = condition

	action = child.get("native_action")
	if action != "":
		edge["action"] = action

	return edge


async def to_interactive(v):
	if not isinstance(v, video.Video):
		raise TypeError("expect " + str(video.Video) + ", got " + str(v.__class__))
	v.__class__ = interactive_video.InteractiveVideo

	util.logv("interactive video " + v.get_bvid())

	edge_map = {}
	node_map = {}

	await util.stall()
	root_edge = await v.get_edge_info()
	util.logt(root_edge)

	choice_theme = root_edge.get("edges").get("skin", {})
	vars_list = []
	for var in root_edge.get("hidden_vars", []):
		vid = var.get("id_v2")
		name = var.get("name")
		value = var.get("value")
		random = (var.get("type") == 2)
		show = (var.get("is_show") > 0)
		util.logv("var " + name, "value " + (str(value) if not random else "random"), "show " + str(show))
		vars_list.append({
			"id":		vid,
			"name":		name,
			"value":	value,
			"random":	random,
			"show":		show,
		})

	root_cid = await v.get_cid()

	edge_queue = deque()
	edge_queue.append((root_cid, root_edge))

	while len(edge_queue) > 0:
		cid, edge = edge_queue.popleft()
		title = edge.get("title")
		util.logv("node " + title, "cid " + str(cid))
		util.logt(edge)

		if cid in node_map:
			continue

		node = make_node(cid, edge)
		node_map[cid] = node

		if node.get("is_leaf"):
			continue

		for child in edge.get("edges").get("questions")[0].get("choices"):
			child_cid = child.get("cid")
			eid = child.get("id")
			util.logv("edge " + child.get("option"), "eid " + str(eid), "target " + str(child_cid))
			edge_map[eid] = make_edge(cid, child)

			if child_cid in node_map:
				continue

			await util.stall()
			child_edge = await v.get_edge_info(eid)
			assert(child_edge.get("edge_id") == eid)
			edge_queue.append((child_cid, child_edge))

	util.logv("node " + str(len(node_map)), "edge " + str(len(edge_map)))

	result = {
		"nodes":	list(node_map.values()),
		"edges":	list(edge_map.values()),
		"vars":		vars_list,
		"theme":	choice_theme
	}
	util.logt(result)
	return result


def save_graph(info, path):
	node_list = info.get("nodes")
	edge_list = info.get("edges")
	vars_list = info.get("vars")
	util.logv("save graph into " + path, "node " + str(len(node_list)), "edge " + str(len(edge_list)))

	with util.staged_file(path, "w") as f:
		f.write("digraph {\n")
		for edge in edge_list:
			util.logt(edge)
			line = str(edge.get("src_cid")) + " -> " + str(edge.get("dst_cid"))
			condition = edge.get("condition", None)
			action = edge.get("action", None)
			if condition or action:
				for var in vars_list:
					vid = var.get("id")
					name = var.get("name")
					condition = condition and condition.replace(vid, name)
					action = action and action.replace(vid, name)

				line = line + "\t[label=\"" + (condition or "")
				if condition and action:
					line = line + '\n'
				line = line + (action or "") + "\"]"

			f.write('\t' + line + ";\n")

		for node in node_list:
			util.logt(node)
			f.write('\t' + str(node.get("cid")) + "\t[label=\"" + node.get("title") + "\"];\n")

		f.write("}\n")

