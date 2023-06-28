#!/usr/bin/env python

import json
import os
import requests
import sys


sys.path.append('/opt')

import moses_common.__init__ as common
import moses_common.api_gateway
import moses_common.dynamodb
import moses_common.s3
import moses_common.sinkinai
import moses_common.ui
import moses_common.visual_artists as visual_artists


ui = moses_common.ui.Interface(usage_message="""
Generate an image based on a known artist using a generated prompt.

  generator.py
  
  Options:
    -r, --refresh               Refresh artist list.
    
    -h, --help                  This help screen.
    -v, --verbose               More output.
    -x, --extra_verbose         Even more output.
""")

log_level = 5
dry_run = False


def handler(event, context):
	global log_level
	global dry_run
	global ui
	
	print("event {}: {}".format(type(event), event))
	api = moses_common.api_gateway.Request(event)
	
	path = api.parse_path()
	print("path:", path)
	
	method = api.method
	print("method:", method)
	
	query, metadata = api.process_query()
	body = api.body
	
	if method == 'GET':
		print("query:", query)
		print("metadata:", metadata)
	else:
		print("body:", body)
	
	output = {}
	if 'opts' in event and event['opts']['retrieve']:
		collective = visual_artists.Collective(log_level=log_level, dry_run=dry_run)
		collective.retrieve_artist_list()
	else:
		output['filename'] = generate()
	
	if path[0] != 'api':
		return { "statusCode": 500, "body": "Invalid API" }
	
	output = {}
# 	if len(path) >= 2:
# 		# /inventory/build
# 		if path[1] == 'build':
# 			if method == 'GET':
# 				output = get_build(event)
# 			else:
# 				output = { "status": 405, "error": "Method not allowed" }
# 		
# 		# /inventory/transfers
# 		elif path[1] == 'transfers':
# 			if method == 'GET':
# 				output = get_transfers(api, inventory, query, metadata)
# 			elif method == 'POST':
# 				output = post_transfers(api, inventory, body, event)
# 	# /inventory
# 	else:
# 		if method == 'GET':
# 			output = get_inventory(api, inventory, query, metadata)
# 		else:
# 			output = { "status": 405, "error": "Method not allowed" }
	
	
	if type(output) is dict and 'status' in output and 'error' in output:
		print("ERROR: {} - {}".format(output['status'], output['error']))
		return {
			"statusCode": output['status'],
			"body": output['error']
		}
	
	if type(output) is list:
		output = {
			'count': len(output),
			'results': output
		}
	response = {
		"statusCode": 200,
		"body": common.make_json(output)
	}
	
#	print("response {}: {}".format(type(response), response))
	return response



def generate():
	global log_level
	global dry_run
	
	collective = visual_artists.Collective(log_level=log_level, dry_run=dry_run)
	
	artist = collective.choose_artist()
	
	query = artist.get_query()
	
	prompt = visual_artists.Prompt(query, log_level=log_level, dry_run=dry_run)
	prompt.generate()
	
	neg_prompt = prompt.get_negative_prompt('sinkin')
	
	filename_prefix = None
	if prompt.data and 'query' in prompt.data and 'artist' in prompt.data['query']:
		filename_prefix = common.convert_to_snakecase(common.normalize(prompt.data['query']['artist']))
	
	save_directory = os.environ.get('HOME') + '/Downloads'
	sinkinai = moses_common.sinkinai.SinkinAI(save_directory=save_directory, model='Deliberate', log_level=log_level, dry_run=dry_run)
	
	model = None
	if prompt.data and 'query' in prompt.data and 'model' in prompt.data['query']:
		model = prompt.data['query']['model']
	
	data = sinkinai.text_to_image(
		prompt.prompt,
		model=model,
		negative_prompt=neg_prompt,
# 		seed=opts['seed'],
# 		steps=opts['steps'],
# 		cfg_scale=opts['cfg'],
		width=prompt.data['width'],
		height=prompt.data['height'],
		filename_prefix=filename_prefix,
		return_args=True
	)
	if prompt.data and 'query' in prompt.data:
		data['query'] = prompt.data['query']
	data['orientation'] = prompt.data.get('orientation', 'square')
	data['aspect'] = prompt.data.get('aspect', 'square')
	
	# Generate image
	success, data = sinkinai.text_to_image(data)
	
	# Send to s3
	bucket = moses_common.s3.Bucket('artintelligence.gallery')
	file = moses_common.s3.Object(bucket, f"www/images/{data['filename']}")
	response = file.upload_file(data['filepath'])
	
	del(data['filepath'])
	table = moses_common.dynamodb.Table('artintelligence.gallery-images')
	table.put_item(data)


def main():
	global log_level
	global dry_run
	global ui
	
	args, opts = ui.get_options({
		"args": [ {
			"name": "prompt",
			"label": "Text image prompt"
		}],
		"options": [ {
			"short": "v",
			"long": "verbose"
		}, {
			"short": "x",
			"long": "extra_verbose"
		}, {
			"long": "dry_run"
		},
		
		{
			"short": "r",
			"long": "retrieve"
		} ]
	})

	dry_run = False
	if opts['dry_run']:
		dry_run = True

	log_level = 5
	if opts['v']:
		log_level = 6
	elif opts['x']:
		log_level = 7
	
	if opts['retrieve']:
		retrieve_artist_list()
		ui.success("Retrieved artist list")
		return
	
	handler({
		"args": args,
		"opts": opts
	}, {})

main()

